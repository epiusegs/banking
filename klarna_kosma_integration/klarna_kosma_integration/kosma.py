# Copyright (c) 2022, ALYF GmbH and contributors
# For license information, please see license.txt
from typing import Dict

import frappe

from frappe import _

from klarna_kosma_integration.connectors.klarna_kosma_connector import (
	KosmaError,
)
from klarna_kosma_integration.connectors.klarna_kosma_consent import (
	KlarnaKosmaConsent,
)
from klarna_kosma_integration.connectors.klarna_kosma_flow import (
	KlarnaKosmaFlow,
)
from klarna_kosma_integration.connectors.kosma_transaction import (
	KosmaTransaction,
)
from klarna_kosma_integration.klarna_kosma_integration.utils import (
	add_bank,
	account_last_sync_date,
	create_bank_transactions,
	create_session_doc,
	exchange_consent_token,
	get_consent_data,
	get_session_flow_ids,
)


class Kosma:
	"""An ERPNext wrapper over Kosma Connectors"""

	def get_client_token(self, current_flow: str) -> Dict:
		settings = frappe.get_single("Klarna Kosma Settings")

		flow = KlarnaKosmaFlow(env=settings.env, api_token=settings.get_password("api_token"))
		session_details = self.start_session(flow)
		flow_data = self.start_flow(flow, current_flow, session_details)

		session_data = {
			"session_id_short": session_details.get("session_id_short"),
			"client_token": flow_data.get("client_token"),
		}
		return session_data

	def flow_accounts(self, session_id_short: str) -> Dict:
		try:
			session_id, flow_id = get_session_flow_ids(session_id_short)
			settings = frappe.get_single("Klarna Kosma Settings")

			flow = KlarnaKosmaFlow(
				env=settings.env, api_token=settings.get_password("api_token")
			)
			accounts_data = flow.accounts(session_id, flow_id)  # Fetch Accounts
			flow.raise_for_status(accounts_data)

			frappe.db.set_value(
				"Klarna Kosma Session", session_id_short, "flow_state", "FINISHED"
			)

			bank_name = self.get_session_bank(flow, session_id)
			accounts_data["result"]["bank_name"] = bank_name

			# Get and store Bank Consent in Bank record
			consent = flow.get_consent(session_id)
			self.set_consent(consent, bank_name)

			return accounts_data
		except Exception as exc:
			self.handle_exception(exc, _("Failed to get Bank Accounts."))
		finally:
			self.end_session(flow, session_id, session_id_short)

	def consent_accounts(self, bank: str):
		try:
			consent_id, consent_token = get_consent_data(bank)

			settings = frappe.get_single("Klarna Kosma Settings")
			consent = KlarnaKosmaConsent(
				env=settings.env, api_token=settings.get_password("api_token")
			)
			accounts = consent.accounts(consent_id, consent_token)

			exchange_consent_token(accounts, bank)
			consent.raise_for_status(accounts)

			accounts = accounts.get("result", {}).get("accounts")
			return accounts
		except Exception as exc:
			self.handle_exception(exc, _("Failed to get Bank Accounts."))

	def consent_transactions(self, account: str, start_date: str):
		account_id, bank = frappe.db.get_value(
			"Bank Account", account, ["kosma_account_id", "bank"]
		)
		consent_id, consent_token = get_consent_data(bank)
		settings = frappe.get_single("Klarna Kosma Settings")

		consent = KlarnaKosmaConsent(
			env=settings.env, api_token=settings.get_password("api_token")
		)

		next_page, url, offset = True, None, None
		try:
			while next_page:
				transactions = consent.transactions(
					account_id, start_date, consent_id, consent_token, url, offset
				)

				new_consent_token = exchange_consent_token(transactions, bank)
				consent.raise_for_status(transactions)

				# Process Request Response
				transaction = KosmaTransaction(transactions)
				next_page = transaction.is_next_page()
				if next_page:
					url, offset = transaction.next_page_request()
					consent_token = new_consent_token

				if transaction.transaction_list:
					create_bank_transactions(account, transaction.transaction_list)
		except Exception as exc:
			self.handle_exception(exc, _("Failed to get Kosma Transactions."))

	def start_session(self, flow_obj: "KlarnaKosmaFlow") -> Dict:
		try:
			session_details = flow_obj.start_session()
			flow_obj.raise_for_status(session_details)
			create_session_doc(session_details)
			return session_details
		except Exception as exc:
			self.handle_exception(exc, _("Failed to start Kosma Session."))

	def end_session(
		self, flow_obj: "KlarnaKosmaFlow", session_id: str, session_id_short: str
	) -> None:
		try:
			flow_obj.end_session(session_id)
			frappe.db.set_value("Klarna Kosma Session", session_id_short, "status", "Closed")
			frappe.db.commit()
		except Exception as exc:
			self.handle_exception(exc, _("Failed to end Kosma session"))

	def start_flow(
		self, flow_obj: "KlarnaKosmaFlow", current_flow: str, session: Dict
	) -> Dict:
		try:
			flow_data = flow_obj.start(flow_type=current_flow, flows=session.get("flows"))
			flow_obj.raise_for_status(flow_data)

			# Update Flow info in Session Doc
			session_id_short = session.get("session_id_short")
			session_doc = frappe.get_doc("Klarna Kosma Session", session_id_short)
			session_doc.update(
				{
					"flow_id": flow_data.get("flow_id"),
					"flow_state": flow_data.get("state"),
				}
			)
			session_doc.save()
			return flow_data
		except Exception as exc:
			self.handle_exception(exc, _("Failed to start Kosma Flow."))

	def get_session_bank(self, flow_obj: "KlarnaKosmaFlow", session_id: str):
		"""Get Bank name from session and create Bank record if absent."""
		try:
			session = flow_obj.get_session(session_id)
			flow_obj.raise_for_status(session)

			bank_data = session.get("bank", {})
			bank_name = add_bank(bank_data)
			return bank_name
		except Exception as exc:
			self.handle_exception(exc, _("Failed to get Kosma Session"))

	def set_consent(self, consent: Dict, bank_name: str) -> None:
		bank_doc = frappe.get_doc("Bank", bank_name)
		bank_doc.update(consent)
		bank_doc.save()

	def handle_exception(self, exc, error_msg: str):
		frappe.log_error(title=_("Kosma Error"), message=frappe.get_traceback())

		if isinstance(exc, KosmaError):
			error_msg = exc.message

		frappe.throw(title=_("Kosma Error"), msg=error_msg)


@frappe.whitelist()
def sync_kosma_transactions(account: str):
	"""Fetch and insert paginated Kosma transactions"""
	start_date = account_last_sync_date(account)
	Kosma().consent_transactions(account, start_date)