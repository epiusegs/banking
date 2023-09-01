# Copyright (c) 2023, ALYF GmbH and contributors
# For license information, please see license.txt
import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.custom import ConstantColumn
from frappe.utils import cint, flt
from pypika.terms import Parameter

from erpnext import get_default_cost_center
from erpnext.accounts.doctype.bank_transaction.bank_transaction import (
	get_total_allocated_amount,
)
from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
	reconcile_vouchers,
)
from erpnext.accounts.utils import get_account_currency


class BankReconciliationToolBeta(Document):
	pass


@frappe.whitelist()
def get_bank_transactions(
	bank_account, from_date=None, to_date=None, order_by="date asc"
):
	# returns bank transactions for a bank account
	filters = []
	filters.append(["bank_account", "=", bank_account])
	filters.append(["docstatus", "=", 1])
	filters.append(["unallocated_amount", ">", 0.0])
	if to_date:
		filters.append(["date", "<=", to_date])
	if from_date:
		filters.append(["date", ">=", from_date])
	transactions = frappe.get_all(
		"Bank Transaction",
		fields=[
			"date",
			"deposit",
			"withdrawal",
			"currency",
			"description",
			"name",
			"bank_account",
			"company",
			"unallocated_amount",
			"reference_number",
			"party_type",
			"party",
			"bank_party_name",
			"bank_party_account_number",
			"bank_party_iban",
		],
		filters=filters,
		order_by=order_by,
	)
	return transactions


@frappe.whitelist()
def create_journal_entry_bts(
	bank_transaction_name,
	reference_number=None,
	reference_date=None,
	posting_date=None,
	entry_type=None,
	second_account=None,
	mode_of_payment=None,
	party_type=None,
	party=None,
	allow_edit=None,
):
	# Create a new journal entry based on the bank transaction
	bank_transaction = frappe.db.get_values(
		"Bank Transaction",
		bank_transaction_name,
		fieldname=["name", "deposit", "withdrawal", "bank_account"],
		as_dict=True,
	)[0]
	company_account = frappe.get_value(
		"Bank Account", bank_transaction.bank_account, "account"
	)
	account_type = frappe.db.get_value("Account", second_account, "account_type")
	if account_type in ["Receivable", "Payable"]:
		if not (party_type and party):
			frappe.throw(
				_("Party Type and Party is required for Receivable / Payable account {0}").format(
					second_account
				)
			)

	company = frappe.get_value("Account", company_account, "company")

	accounts = []
	# Multi Currency?
	accounts.append(
		{
			"account": second_account,
			"credit_in_account_currency": bank_transaction.deposit,
			"debit_in_account_currency": bank_transaction.withdrawal,
			"party_type": party_type,
			"party": party,
			"cost_center": get_default_cost_center(company),
		}
	)

	accounts.append(
		{
			"account": company_account,
			"bank_account": bank_transaction.bank_account,
			"credit_in_account_currency": bank_transaction.withdrawal,
			"debit_in_account_currency": bank_transaction.deposit,
			"cost_center": get_default_cost_center(company),
		}
	)

	journal_entry_dict = {
		"voucher_type": entry_type,
		"company": company,
		"posting_date": posting_date,
		"cheque_date": reference_date,
		"cheque_no": reference_number,
		"mode_of_payment": mode_of_payment,
	}
	journal_entry = frappe.new_doc("Journal Entry")
	journal_entry.update(journal_entry_dict)
	journal_entry.set("accounts", accounts)
	journal_entry.insert()

	if allow_edit:
		return journal_entry  # Return saved document

	journal_entry.submit()

	if bank_transaction.deposit > 0.0:
		paid_amount = bank_transaction.deposit
	else:
		paid_amount = bank_transaction.withdrawal

	vouchers = json.dumps(
		[
			{
				"payment_doctype": "Journal Entry",
				"payment_name": journal_entry.name,
				"amount": paid_amount,
			}
		]
	)

	return reconcile_vouchers(bank_transaction_name, vouchers)


@frappe.whitelist()
def create_payment_entry_bts(
	bank_transaction_name,
	reference_number=None,
	reference_date=None,
	party_type=None,
	party=None,
	posting_date=None,
	mode_of_payment=None,
	project=None,
	cost_center=None,
	allow_edit=None,
):
	# Create a new payment entry based on the bank transaction
	bank_transaction = frappe.db.get_values(
		"Bank Transaction",
		bank_transaction_name,
		fieldname=["name", "unallocated_amount", "deposit", "bank_account"],
		as_dict=True,
	)[0]
	paid_amount = bank_transaction.unallocated_amount
	payment_type = "Receive" if bank_transaction.deposit > 0.0 else "Pay"

	company_account = frappe.get_value(
		"Bank Account", bank_transaction.bank_account, "account"
	)
	company = frappe.get_value("Account", company_account, "company")
	payment_entry_dict = {
		"company": company,
		"payment_type": payment_type,
		"reference_no": reference_number,
		"reference_date": reference_date,
		"party_type": party_type,
		"party": party,
		"posting_date": posting_date,
		"paid_amount": paid_amount,
		"received_amount": paid_amount,
	}
	payment_entry = frappe.new_doc("Payment Entry")

	payment_entry.update(payment_entry_dict)

	if mode_of_payment:
		payment_entry.mode_of_payment = mode_of_payment
	if project:
		payment_entry.project = project
	if cost_center:
		payment_entry.cost_center = cost_center
	if payment_type == "Receive":
		payment_entry.paid_to = company_account
	else:
		payment_entry.paid_from = company_account

	payment_entry.validate()
	payment_entry.insert()

	if allow_edit:
		return payment_entry  # Return saved document

	payment_entry.submit()
	vouchers = json.dumps(
		[
			{
				"payment_doctype": "Payment Entry",
				"payment_name": payment_entry.name,
				"amount": paid_amount,
			}
		]
	)
	return reconcile_vouchers(bank_transaction_name, vouchers)


@frappe.whitelist()
def upload_bank_statement(**args):
	args = frappe._dict(args)
	bsi = frappe.new_doc("Bank Statement Import")

	if args.company:
		bsi.update(
			{
				"company": args.company,
			}
		)

	if args.bank_account:
		bsi.update({"bank_account": args.bank_account})

	bsi.save()
	return bsi  # Return saved document


@frappe.whitelist()
def auto_reconcile_vouchers(
	bank_account,
	from_date=None,
	to_date=None,
	filter_by_reference_date=None,
	from_reference_date=None,
	to_reference_date=None,
):
	# Auto reconcile vouchers with matching reference numbers
	frappe.flags.auto_reconcile_vouchers = True
	reconciled, partially_reconciled = set(), set()

	bank_transactions = get_bank_transactions(bank_account, from_date, to_date)
	for transaction in bank_transactions:
		linked_payments = get_linked_payments(
			transaction.name,
			["payment_entry", "journal_entry"],
			from_date,
			to_date,
			filter_by_reference_date,
			from_reference_date,
			to_reference_date,
		)

		if not linked_payments:
			continue

		vouchers = list(
			map(
				lambda entry: {
					"payment_doctype": entry.get("doctype"),
					"payment_name": entry.get("name"),
					"amount": entry.get("paid_amount"),
				},
				linked_payments,
			)
		)

		unallocated_before = transaction.unallocated_amount
		transaction = reconcile_vouchers(transaction.name, json.dumps(vouchers))

		if transaction.status == "Reconciled":
			reconciled.add(transaction.name)
		elif flt(unallocated_before) != flt(transaction.unallocated_amount):
			partially_reconciled.add(transaction.name)  # Partially reconciled

	alert_message, indicator = "", "blue"
	if not partially_reconciled and not reconciled:
		alert_message = _("No matches occurred via auto reconciliation")

	if reconciled:
		alert_message += _("{0} Transaction(s) Reconciled").format(len(reconciled))
		alert_message += "<br>"
		indicator = "green"

	if partially_reconciled:
		alert_message += _("{0} {1} Partially Reconciled").format(
			len(partially_reconciled),
			_("Transactions") if len(partially_reconciled) > 1 else _("Transaction"),
		)
		indicator = "green"

	frappe.msgprint(title=_("Auto Reconciliation"), msg=alert_message, indicator=indicator)
	frappe.flags.auto_reconcile_vouchers = False
	return reconciled, partially_reconciled


@frappe.whitelist()
def get_linked_payments(
	bank_transaction_name,
	document_types=None,
	from_date=None,
	to_date=None,
	filter_by_reference_date=None,
	from_reference_date=None,
	to_reference_date=None,
):
	# get all matching payments for a bank transaction
	transaction = frappe.get_doc("Bank Transaction", bank_transaction_name)
	gl_account, company = frappe.db.get_value(
		"Bank Account", transaction.bank_account, ["account", "company"]
	)
	matching = check_matching(
		gl_account,
		company,
		transaction,
		document_types,
		from_date,
		to_date,
		filter_by_reference_date,
		from_reference_date,
		to_reference_date,
	)
	return subtract_allocations(gl_account, matching)


def subtract_allocations(gl_account, vouchers):
	"Look up & subtract any existing Bank Transaction allocations"
	copied = []
	for voucher in vouchers:
		rows = get_total_allocated_amount(voucher.get("doctype"), voucher.get("name"))
		amount = None
		for row in rows:
			if row["gl_account"] == gl_account:
				amount = row["total"]
				break

		if amount:
			voucher["paid_amount"] -= amount

		copied.append(voucher)
	return copied


def check_matching(
	bank_account,
	company,
	transaction,
	document_types,
	from_date,
	to_date,
	filter_by_reference_date,
	from_reference_date,
	to_reference_date,
):
	# combine all types of vouchers
	subquery = get_queries(
		bank_account,
		company,
		transaction,
		document_types,
		from_date,
		to_date,
		filter_by_reference_date,
		from_reference_date,
		to_reference_date,
	)
	filters = {
		"amount": transaction.unallocated_amount,
		"payment_type": "Receive" if transaction.deposit > 0.0 else "Pay",
		"reference_no": transaction.reference_number,
		"party_type": transaction.party_type,
		"party": transaction.party,
		"bank_account": bank_account,
	}

	matching_vouchers = []
	matching_vouchers.extend(
		get_loan_vouchers(bank_account, transaction, document_types, filters)
	)

	for query in subquery:
		matching_vouchers.extend(
			frappe.db.sql(
				query,
				filters,
				as_dict=1,
			)
		)

	if not matching_vouchers:
		return []

	for voucher in matching_vouchers:
		# higher rank if voucher name is in bank transaction
		if voucher["name"] in transaction.description:
			voucher["rank"] += 1
			voucher["name_in_desc_match"] = 1

	return sorted(matching_vouchers, key=lambda x: x["rank"], reverse=True)


def get_queries(
	bank_account,
	company,
	transaction,
	document_types,
	from_date,
	to_date,
	filter_by_reference_date,
	from_reference_date,
	to_reference_date,
):
	# get queries to get matching vouchers
	account_from_to = "paid_to" if transaction.deposit > 0.0 else "paid_from"
	exact_match = "exact_match" in document_types
	queries = []

	# get matching queries from all the apps (except erpnext, to override)
	for method_name in frappe.get_hooks("get_matching_queries")[1:]:
		queries.extend(
			frappe.get_attr(method_name)(
				bank_account,
				company,
				transaction,
				document_types,
				exact_match,
				account_from_to,
				from_date,
				to_date,
				filter_by_reference_date,
				from_reference_date,
				to_reference_date,
			)
			or []
		)

	return queries


def get_matching_queries(
	bank_account,
	company,
	transaction,
	document_types,
	exact_match,
	account_from_to,
	from_date,
	to_date,
	filter_by_reference_date,
	from_reference_date,
	to_reference_date,
):
	queries = []
	exact_party_match = "exact_party_match" in document_types
	currency = get_account_currency(bank_account)

	if "payment_entry" in document_types:
		query = get_pe_matching_query(
			exact_match,
			account_from_to,
			transaction,
			from_date,
			to_date,
			filter_by_reference_date,
			from_reference_date,
			to_reference_date,
			exact_party_match,
		)
		queries.append(query)

	if "journal_entry" in document_types:
		query = get_je_matching_query(
			exact_match,
			transaction,
			from_date,
			to_date,
			filter_by_reference_date,
			from_reference_date,
			to_reference_date,
		)
		queries.append(query)

	if transaction.deposit > 0.0 and "sales_invoice" in document_types:
		if "unpaid_invoices" in document_types:
			query = get_unpaid_si_matching_query(exact_match, exact_party_match, currency)
			queries.append(query)
		else:
			query = get_si_matching_query(exact_match, exact_party_match, currency)
			queries.append(query)

	if transaction.withdrawal > 0.0 and "purchase_invoice" in document_types:
		if "unpaid_invoices" in document_types:
			query = get_unpaid_pi_matching_query(exact_match, exact_party_match, currency)
			queries.append(query)
		else:
			query = get_pi_matching_query(exact_match, exact_party_match, currency)
			queries.append(query)

	if "bank_transaction" in document_types:
		query = get_bt_matching_query(exact_match, transaction, exact_party_match)
		queries.append(query)

	return queries


def get_loan_vouchers(bank_account, transaction, document_types, filters):
	vouchers = []
	exact_match = "exact_match" in document_types

	if transaction.withdrawal > 0.0 and "loan_disbursement" in document_types:
		vouchers.extend(get_ld_matching_query(bank_account, exact_match, filters))

	if transaction.deposit > 0.0 and "loan_repayment" in document_types:
		vouchers.extend(get_lr_matching_query(bank_account, exact_match, filters))

	return vouchers


def get_bt_matching_query(exact_match, transaction, exact_party_match):
	# get matching bank transaction query
	# find bank transactions in the same bank account with opposite sign
	# same bank account must have same company and currency
	bt = frappe.qb.DocType("Bank Transaction")
	field = "deposit" if transaction.withdrawal > 0.0 else "withdrawal"

	ref_rank = (
		frappe.qb.terms.Case()
		.when(bt.reference_number == transaction.reference_number, 1)
		.else_(0)
	)
	unallocated_rank = (
		frappe.qb.terms.Case()
		.when(bt.unallocated_amount == transaction.unallocated_amount, 1)
		.else_(0)
	)

	amount_equality = getattr(bt, field) == transaction.unallocated_amount
	amount_rank = frappe.qb.terms.Case().when(amount_equality, 1).else_(0)

	party_condition = (
		(bt.party_type == transaction.party_type)
		& (bt.party == transaction.party)
		& bt.party.isnotnull()
	)
	party_rank = frappe.qb.terms.Case().when(party_condition, 1).else_(0)
	amount_condition = amount_equality if exact_match else getattr(bt, field) > 0.0

	query = (
		frappe.qb.from_(bt)
		.select(
			(ref_rank + amount_rank + party_rank + unallocated_rank + 1).as_("rank"),
			ConstantColumn("Bank Transaction").as_("doctype"),
			bt.name,
			bt.unallocated_amount.as_("paid_amount"),
			bt.reference_number.as_("reference_no"),
			bt.date.as_("reference_date"),
			bt.party,
			bt.party_type,
			bt.date.as_("posting_date"),
			bt.currency,
			ref_rank.as_("reference_number_match"),
			amount_rank.as_("amount_match"),
			party_rank.as_("party_match"),
			unallocated_rank.as_("unallocated_amount_match"),
		)
		.where(bt.status != "Reconciled")
		.where(bt.name != transaction.name)
		.where(bt.bank_account == transaction.bank_account)
		.where(amount_condition)
		.where(bt.docstatus == 1)
	)

	if exact_party_match:
		query = query.where(party_condition)

	return str(query)


def get_ld_matching_query(bank_account, exact_match, filters):
	loan_disbursement = frappe.qb.DocType("Loan Disbursement")
	matching_reference = loan_disbursement.reference_number == filters.get(
		"reference_number"
	)
	matching_party = loan_disbursement.applicant_type == filters.get(
		"party_type"
	) and loan_disbursement.applicant == filters.get("party")

	rank = frappe.qb.terms.Case().when(matching_reference, 1).else_(0)

	rank1 = frappe.qb.terms.Case().when(matching_party, 1).else_(0)

	query = (
		frappe.qb.from_(loan_disbursement)
		.select(
			rank + rank1 + 1,
			ConstantColumn("Loan Disbursement").as_("doctype"),
			loan_disbursement.name,
			loan_disbursement.disbursed_amount.as_("paid_amount"),
			loan_disbursement.reference_number.as_("reference_no"),
			loan_disbursement.reference_date,
			loan_disbursement.applicant.as_("party"),
			loan_disbursement.applicant_type.as_("party_type"),
			loan_disbursement.disbursement_date.as_("posting_date"),
			"".as_("currency"),
			rank.as_("reference_number_match"),
			rank1.as_("party_match"),
		)
		.where(loan_disbursement.docstatus == 1)
		.where(loan_disbursement.clearance_date.isnull())
		.where(loan_disbursement.disbursement_account == bank_account)
	)

	if exact_match:
		query.where(loan_disbursement.disbursed_amount == filters.get("amount"))
	else:
		query.where(loan_disbursement.disbursed_amount > 0.0)

	vouchers = query.run(as_list=True)

	return vouchers


def get_lr_matching_query(bank_account, exact_match, filters):
	loan_repayment = frappe.qb.DocType("Loan Repayment")
	matching_reference = loan_repayment.reference_number == filters.get("reference_number")
	matching_party = loan_repayment.applicant_type == filters.get(
		"party_type"
	) and loan_repayment.applicant == filters.get("party")

	rank = frappe.qb.terms.Case().when(matching_reference, 1).else_(0)

	rank1 = frappe.qb.terms.Case().when(matching_party, 1).else_(0)

	query = (
		frappe.qb.from_(loan_repayment)
		.select(
			rank + rank1 + 1,
			ConstantColumn("Loan Repayment").as_("doctype"),
			loan_repayment.name,
			loan_repayment.amount_paid.as_("paid_amount"),
			loan_repayment.reference_number.as_("reference_no"),
			loan_repayment.reference_date,
			loan_repayment.applicant.as_("party"),
			loan_repayment.applicant_type.as_("party_type"),
			loan_repayment.posting_date,
			"".as_("currency"),
			rank.as_("reference_number_match"),
			rank1.as_("party_match"),
		)
		.where(loan_repayment.docstatus == 1)
		.where(loan_repayment.clearance_date.isnull())
		.where(loan_repayment.payment_account == bank_account)
	)

	if frappe.db.has_column("Loan Repayment", "repay_from_salary"):
		query = query.where((loan_repayment.repay_from_salary == 0))

	if exact_match:
		query.where(loan_repayment.amount_paid == filters.get("amount"))
	else:
		query.where(loan_repayment.amount_paid > 0.0)

	vouchers = query.run()

	return vouchers


def get_pe_matching_query(
	exact_match,
	account_from_to,
	transaction,
	from_date,
	to_date,
	filter_by_reference_date,
	from_reference_date,
	to_reference_date,
	exact_party_match,
):
	to_from = "to" if transaction.deposit > 0.0 else "from"
	currency_field = f"paid_{to_from}_account_currency"
	payment_type = "Receive" if transaction.deposit > 0.0 else "Pay"
	pe = frappe.qb.DocType("Payment Entry")

	ref_condition = pe.reference_no == transaction.reference_number
	ref_rank = frappe.qb.terms.Case().when(ref_condition, 1).else_(0)

	amount_equality = pe.paid_amount == transaction.unallocated_amount
	amount_rank = frappe.qb.terms.Case().when(amount_equality, 1).else_(0)
	amount_condition = amount_equality if exact_match else pe.paid_amount > 0.0

	party_condition = (
		(pe.party_type == transaction.party_type)
		& (pe.party == transaction.party)
		& pe.party.isnotnull()
	)
	party_rank = frappe.qb.terms.Case().when(party_condition, 1).else_(0)

	filter_by_date = pe.posting_date.between(from_date, to_date)
	if cint(filter_by_reference_date):
		filter_by_date = pe.reference_date.between(from_reference_date, to_reference_date)

	query = (
		frappe.qb.from_(pe)
		.select(
			(ref_rank + amount_rank + party_rank + 1).as_("rank"),
			ConstantColumn("Payment Entry").as_("doctype"),
			pe.name,
			pe.paid_amount,
			pe.reference_no,
			pe.reference_date,
			pe.party,
			pe.party_type,
			pe.posting_date,
			getattr(pe, currency_field).as_("currency"),
			ref_rank.as_("reference_number_match"),
			amount_rank.as_("amount_match"),
			party_rank.as_("party_match"),
		)
		.where(pe.docstatus == 1)
		.where(pe.payment_type.isin([payment_type, "Internal Transfer"]))
		.where(pe.clearance_date.isnull())
		.where(getattr(pe, account_from_to) == Parameter("%(bank_account)s"))
		.where(amount_condition)
		.where(filter_by_date)
		.orderby(pe.reference_date if cint(filter_by_reference_date) else pe.posting_date)
	)

	if frappe.flags.auto_reconcile_vouchers == True:
		query = query.where(ref_condition)
	if exact_party_match:
		query = query.where(party_condition)

	return str(query)


def get_je_matching_query(
	exact_match,
	transaction,
	from_date,
	to_date,
	filter_by_reference_date,
	from_reference_date,
	to_reference_date,
):
	# get matching journal entry query
	# We have mapping at the bank level
	# So one bank could have both types of bank accounts like asset and liability
	# So cr_or_dr should be judged only on basis of withdrawal and deposit and not account type
	cr_or_dr = "credit" if transaction.withdrawal > 0.0 else "debit"
	je = frappe.qb.DocType("Journal Entry")
	jea = frappe.qb.DocType("Journal Entry Account")

	ref_condition = je.cheque_no == transaction.reference_number
	ref_rank = frappe.qb.terms.Case().when(ref_condition, 1).else_(0)

	amount_field = f"{cr_or_dr}_in_account_currency"
	amount_equality = getattr(jea, amount_field) == transaction.unallocated_amount
	amount_rank = frappe.qb.terms.Case().when(amount_equality, 1).else_(0)

	filter_by_date = je.posting_date.between(from_date, to_date)
	if cint(filter_by_reference_date):
		filter_by_date = je.cheque_date.between(from_reference_date, to_reference_date)

	query = (
		frappe.qb.from_(jea)
		.join(je)
		.on(jea.parent == je.name)
		.select(
			(ref_rank + amount_rank + 1).as_("rank"),
			ConstantColumn("Journal Entry").as_("doctype"),
			je.name,
			getattr(jea, amount_field).as_("paid_amount"),
			je.cheque_no.as_("reference_no"),
			je.cheque_date.as_("reference_date"),
			je.pay_to_recd_from.as_("party"),
			jea.party_type,
			je.posting_date,
			jea.account_currency.as_("currency"),
			ref_rank.as_("reference_number_match"),
			amount_rank.as_("amount_match"),
		)
		.where(je.docstatus == 1)
		.where(je.voucher_type != "Opening Entry")
		.where(je.clearance_date.isnull())
		.where(jea.account == Parameter("%(bank_account)s"))
		.where(amount_equality if exact_match else getattr(jea, amount_field) > 0.0)
		.where(je.docstatus == 1)
		.where(filter_by_date)
		.orderby(je.cheque_date if cint(filter_by_reference_date) else je.posting_date)
	)

	if frappe.flags.auto_reconcile_vouchers == True:
		query = query.where(ref_condition)

	return str(query)


def get_si_matching_query(exact_match, exact_party_match, currency):
	# get matching paid sales invoice query
	si = frappe.qb.DocType("Sales Invoice")
	sip = frappe.qb.DocType("Sales Invoice Payment")

	amount_equality = sip.amount == Parameter("%(amount)s")
	amount_rank = frappe.qb.terms.Case().when(amount_equality, 1).else_(0)
	amount_condition = amount_equality if exact_match else sip.amount > 0.0

	party_condition = si.customer == Parameter("%(party)s")
	party_rank = frappe.qb.terms.Case().when(party_condition, 1).else_(0)

	query = (
		frappe.qb.from_(sip)
		.join(si)
		.on(sip.parent == si.name)
		.select(
			(party_rank + amount_rank + 1).as_("rank"),
			ConstantColumn("Sales Invoice").as_("doctype"),
			si.name,
			sip.amount.as_("paid_amount"),
			si.name.as_("reference_no"),
			si.posting_date.as_("reference_date"),
			si.customer.as_("party"),
			ConstantColumn("Customer").as_("party_type"),
			si.posting_date,
			si.currency,
			party_rank.as_("party_match"),
			amount_rank.as_("amount_match"),
		)
		.where(si.docstatus == 1)
		.where(sip.clearance_date.isnull())
		.where(sip.account == Parameter("%(bank_account)s"))
		.where(amount_condition)
		.where(si.currency == currency)
	)

	if exact_party_match:
		query = query.where(party_condition)

	return str(query)


def get_unpaid_si_matching_query(exact_match, exact_party_match, currency):
	sales_invoice = frappe.qb.DocType("Sales Invoice")

	party_condition = sales_invoice.customer == Parameter("%(party)s")
	party_match = frappe.qb.terms.Case().when(party_condition, 1).else_(0)

	grand_total_condition = sales_invoice.grand_total == Parameter("%(amount)s")
	amount_match = frappe.qb.terms.Case().when(grand_total_condition, 1).else_(0)

	query = (
		frappe.qb.from_(sales_invoice)
		.select(
			(party_match + amount_match + 1).as_("rank"),
			ConstantColumn("Sales Invoice").as_("doctype"),
			sales_invoice.name.as_("name"),
			sales_invoice.outstanding_amount.as_("paid_amount"),
			sales_invoice.name.as_("reference_no"),
			sales_invoice.posting_date.as_("reference_date"),
			sales_invoice.customer.as_("party"),
			ConstantColumn("Customer").as_("party_type"),
			sales_invoice.posting_date,
			sales_invoice.currency,
			party_match.as_("party_match"),
			amount_match.as_("amount_match"),
		)
		.where(sales_invoice.docstatus == 1)
		.where(sales_invoice.is_return == 0)
		.where(sales_invoice.outstanding_amount > 0.0)
		.where(sales_invoice.currency == currency)
	)

	if exact_match:
		query = query.where(grand_total_condition)

	if exact_party_match:
		query = query.where(party_condition)

	return str(query)


def get_pi_matching_query(exact_match, exact_party_match, currency):
	# get matching purchase invoice query when they are also used as payment entries (is_paid)
	purchase_invoice = frappe.qb.DocType("Purchase Invoice")

	amount_equality = purchase_invoice.paid_amount == Parameter("%(amount)s")
	amount_rank = frappe.qb.terms.Case().when(amount_equality, 1).else_(0)
	amount_condition = (
		amount_equality if exact_match else purchase_invoice.paid_amount > 0.0
	)

	party_condition = purchase_invoice.supplier == Parameter("%(party)s")
	party_rank = frappe.qb.terms.Case().when(party_condition, 1).else_(0)

	query = (
		frappe.qb.from_(purchase_invoice)
		.select(
			(party_rank + amount_rank + 1).as_("rank"),
			ConstantColumn("Purchase Invoice").as_("doctype"),
			purchase_invoice.name,
			purchase_invoice.paid_amount,
			purchase_invoice.bill_no.as_("reference_no"),
			purchase_invoice.bill_date.as_("reference_date"),
			purchase_invoice.supplier.as_("party"),
			ConstantColumn("Supplier").as_("party_type"),
			purchase_invoice.posting_date,
			purchase_invoice.currency,
			party_rank.as_("party_match"),
			amount_rank.as_("amount_match"),
		)
		.where(purchase_invoice.docstatus == 1)
		.where(purchase_invoice.is_paid == 1)
		.where(purchase_invoice.clearance_date.isnull())
		.where(purchase_invoice.cash_bank_account == Parameter("%(bank_account)s"))
		.where(amount_condition)
		.where(purchase_invoice.currency == currency)
	)

	if exact_party_match:
		query = query.where(party_condition)

	return str(query)


def get_unpaid_pi_matching_query(exact_match, exact_party_match, currency):
	purchase_invoice = frappe.qb.DocType("Purchase Invoice")

	party_condition = purchase_invoice.supplier == Parameter("%(party)s")
	party_match = frappe.qb.terms.Case().when(party_condition, 1).else_(0)

	grand_total_condition = purchase_invoice.grand_total == Parameter("%(amount)s")
	amount_match = frappe.qb.terms.Case().when(grand_total_condition, 1).else_(0)

	query = (
		frappe.qb.from_(purchase_invoice)
		.select(
			(party_match + amount_match + 1).as_("rank"),
			ConstantColumn("Purchase Invoice").as_("doctype"),
			purchase_invoice.name.as_("name"),
			purchase_invoice.outstanding_amount.as_("paid_amount"),
			purchase_invoice.bill_no.as_("reference_no"),
			purchase_invoice.bill_date.as_("reference_date"),
			purchase_invoice.supplier.as_("party"),
			ConstantColumn("Supplier").as_("party_type"),
			purchase_invoice.posting_date,
			purchase_invoice.currency,
			party_match.as_("party_match"),
			amount_match.as_("amount_match"),
		)
		.where(purchase_invoice.docstatus == 1)
		.where(purchase_invoice.is_return == 0)
		.where(purchase_invoice.outstanding_amount > 0.0)
		.where(purchase_invoice.currency == currency)
	)

	if exact_match:
		query = query.where(grand_total_condition)
	if exact_party_match:
		query = query.where(party_condition)

	return str(query)