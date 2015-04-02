# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import validate_email_add, cint
from frappe.email.smtp import SMTPServer
from frappe.email.receive import POP3Server, Email
from poplib import error_proto
import markdown2
from datetime import datetime, timedelta

class EmailAccount(Document):
	def autoname(self):
		"""Set name as `email_account_name` or make title from email id."""
		if not self.email_account_name:
			self.email_account_name = self.email_id.split("@", 1)[0]\
				.replace("_", " ").replace(".", " ").replace("-", " ").title()

			if self.service:
				self.email_account_name = self.email_account_name + " " + self.service

		self.name = self.email_account_name

	def validate(self):
		"""Validate email id and check POP3 and SMTP connections is enabled."""
		if self.email_id:
			validate_email_add(self.email_id, True)

		if frappe.local.flags.in_patch or frappe.local.flags.in_test:
			return

		if not frappe.local.flags.in_install and not frappe.local.flags.in_patch:
			if self.enable_incoming:
				self.get_pop3()

			if self.enable_outgoing:
				self.check_smtp()

		if self.notify_if_unreplied:
			for e in self.get_unreplied_notification_emails():
				validate_email_add(e, True)

	def on_update(self):
		"""Check there is only one default of each type."""
		self.there_must_be_only_one_default()

	def there_must_be_only_one_default(self):
		"""If current Email Account is default, un-default all other accounts."""
		for fn in ("default_incoming", "default_outgoing"):
			if self.get(fn):
				for email_account in frappe.get_all("Email Account",
					filters={fn: 1}):
					if email_account.name==self.name:
						continue
					email_account = frappe.get_doc("Email Account",
						email_account.name)
					email_account.set(fn, 0)
					email_account.save()

	def check_smtp(self):
		"""Checks SMTP settings."""
		if self.enable_outgoing:
			if not self.smtp_server:
				frappe.throw(_("{0} is required").format("SMTP Server"))

			server = SMTPServer(login = self.email_id,
				password = self.password,
				server = self.smtp_server,
				port = cint(self.smtp_port),
				use_ssl = cint(self.use_tls)
			)
			server.sess

	def get_pop3(self):
		"""Returns logged in POP3 connection object."""
		args = {
			"host": self.pop3_server,
			"use_ssl": self.use_ssl,
			"username": self.email_id,
			"password": self.password
		}

		if not self.pop3_server:
			frappe.throw(_("{0} is required").format("POP3 Server"))

		pop3 = POP3Server(frappe._dict(args))
		try:
			pop3.connect()
		except error_proto, e:
			frappe.throw(e.message)

		return pop3

	def receive(self, test_mails=None):
		"""Called by scheduler to receive emails from this EMail account using POP3."""
		if self.enable_incoming:
			if frappe.local.flags.in_test:
				incoming_mails = test_mails
			else:
				pop3 = self.get_pop3()
				incoming_mails = pop3.get_messages()

			exceptions = []
			for raw in incoming_mails:
				try:
					self.insert_communication(raw)

				except Exception:
					frappe.db.rollback()
					exceptions.append(frappe.get_traceback())

				else:
					frappe.db.commit()

		if exceptions:
			raise Exception, frappe.as_json(exceptions)

	def insert_communication(self, raw):
		email = Email(raw)

		communication = frappe.get_doc({
			"doctype": "Communication",
			"subject": email.subject,
			"content": email.content,
			"sent_or_received": "Received",
			"sender_full_name": email.from_real_name,
			"sender": email.from_email,
			"recipients": email.mail.get("To"),
			"email_account": self.name,
			"communication_medium": "Email"
		})

		self.set_thread(communication, email)

		communication.insert(ignore_permissions = 1)

		# save attachments
		email.save_attachments_in_doc(communication)

		if self.enable_auto_reply and getattr(communication, "is_first", False):
			self.send_auto_reply(communication, email)

		# notify all participants of this thread
		# convert content to HTML - by default text parts of replies are used.
		communication.content = markdown2.markdown(communication.content)
		communication.notify(attachments=email.attachments, except_recipient = True)

	def set_thread(self, communication, email):
		"""Appends communication to parent based on thread ID. Will extract
		parent communication and will link the communication to the reference of that
		communication. Also set the status of parent transaction to Open or Replied.

		If no thread id is found and `append_to` is set for the email account,
		it will create a new parent transaction (e.g. Issue)"""
		in_reply_to = (email.mail.get("In-Reply-To") or "").strip(" <>")
		parent = None
		if in_reply_to:
			if "@" in in_reply_to:

				# reply to a communication sent from the system
				in_reply_to = in_reply_to.split("@", 1)[0]
				if frappe.db.exists("Communication", in_reply_to):
					parent = frappe.get_doc("Communication", in_reply_to)

					if parent.reference_name:
						# parent same as parent of last communication
						parent = frappe.get_doc(parent.reference_doctype,
							parent.reference_name)

		if not parent and self.append_to:
			# no parent found, but must be tagged
			# insert parent type doc
			parent = frappe.new_doc(self.append_to)

			if parent.meta.get_field("subject"):
				parent.subject = email.subject

			if parent.meta.get_field("sender"):
				parent.sender = email.from_email

			if hasattr(parent, "set_subject"):
				parent.set_subject(email.subject)

			if hasattr(parent, "set_sender"):
				parent.set_sender(email.from_email)

			parent.flags.ignore_mandatory = True

			try:
				parent.insert(ignore_permissions=True)

			except frappe.DuplicateEntryError:

				if frappe.get_meta(self.append_to).get_field("email_id"):
					# assume that duplicate entry is due to email_id field!
					parent = frappe.get_doc(self.append_to, { "email_id": email.from_email })

				else:
					raise

			communication.is_first = True

		if parent:
			communication.reference_doctype = parent.doctype
			communication.reference_name = parent.name

	def send_auto_reply(self, communication, email):
		"""Send auto reply if set."""
		if self.auto_reply_message:
			communication.set_incoming_outgoing_accounts()

			frappe.sendmail(recipients = [email.from_email],
				sender = self.email_id,
				reply_to = communication.incoming_email_account,
				subject = _("Re: ") + communication.subject,
				content = self.auto_reply_message or \
					 frappe.get_template("templates/emails/auto_reply.html").render(communication.as_dict()),
				reference_doctype = communication.reference_doctype,
				reference_name = communication.reference_name,
				message_id = communication.name,
				unsubscribe_message = _("Leave this conversation"),
				bulk=True)

	def get_unreplied_notification_emails(self):
		"""Return list of emails listed"""
		self.send_notification_to = self.send_notification_to.replace(",", "\n")
		out = [e.strip() for e in self.send_notification_to.split("\n")]
		return out

	def on_trash(self):
		"""Clear communications where email account is linked"""
		frappe.db.sql("update `tabCommunication` set email_account='' where email_account=%s", self.name)

@frappe.whitelist()
def get_append_to(doctype, txt, searchfield, start, page_len, filters):
	if not txt: txt = ""
	return [[d] for d in frappe.get_hooks("email_append_to") if txt in d]

def pull(now=False):
	"""Will be called via scheduler, pull emails from all enabled POP3 email accounts."""
	import frappe.tasks
	for email_account in frappe.get_list("Email Account", filters={"enable_incoming": 1}):
		#frappe.tasks.pull_from_email_account(frappe.local.site, email_account.name)
		if now:
			frappe.tasks.pull_from_email_account(frappe.local.site, email_account.name)
		else:
			frappe.tasks.pull_from_email_account.delay(frappe.local.site, email_account.name)

def notify_unreplied():
	"""Sends email notifications if there are unreplied Communications
		and `notify_if_unreplied` is set as true."""

	for email_account in frappe.get_all("Email Account", "name", filters={"enable_incoming": 1, "notify_if_unreplied": 1}):
		email_account = frappe.get_doc("Email Account", email_account.name)
		if email_account.append_to:

			# get open communications younger than x mins, for given doctype
			for comm in frappe.get_all("Communication", "name", filters={
					"sent_or_received": "Received",
					"reference_doctype": email_account.append_to,
					"unread_notification_sent": 0,
					"creation": ("<", datetime.now() - timedelta(seconds = (email_account.unreplied_for_mins or 30) * 60)),
					"creation": (">", datetime.now() - timedelta(seconds = (email_account.unreplied_for_mins or 30) * 60 * 3))
				}):
				comm = frappe.get_doc("Communication", comm.name)

				if frappe.db.get_value(comm.reference_doctype, comm.reference_name, "status")=="Open":
					# if status is still open
					frappe.sendmail(recipients=email_account.get_unreplied_notification_emails(),
						content=comm.content, subject=comm.subject, doctype= comm.reference_doctype,
						name=comm.reference_name, bulk=True)

				# update flag
				comm.db_set("unread_notification_sent", 1)
