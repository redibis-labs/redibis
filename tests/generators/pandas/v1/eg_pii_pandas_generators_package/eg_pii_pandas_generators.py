"""
Egypt Telecom, E-commerce, and Fintech PII Synthetic Data Utilities for pandas.

This module is the pandas equivalent of the PySpark utility package generated for
an Egyptian PII golden-data catalogue. It exposes two public functions for each
of the 14 table schemas:

1. ``generate_<table_name>(num_rows, seed=42)`` returns a pandas DataFrame with
   realistic but entirely synthetic values aligned to the table schema.
2. ``load_<table_name>_csv(csv_path, existing_df=None, target_path=None, ...)``
   reads records from a CSV file, coerces/reorders columns to the expected
   schema, optionally combines them with an existing DataFrame, and optionally
   writes the result to a file.

The generated records are synthetic and are intended for PII scanner evaluation,
classification benchmarking, anonymization tests, data-contract validation, and
agent-driven tool testing. They must not be treated as real customer data.

Agent-tool usage hint:
    Agents can call ``describe_table(table_name)`` and ``get_table_columns`` to
    understand expected inputs before invoking ``generate_table`` or
    ``load_table_csv``. For user-uploaded CSV files, use the table-specific
    ``load_*_csv`` function with ``validate_required=True`` and
    ``strict_columns=False`` unless the test intentionally checks schema failure.
"""

from __future__ import annotations

import hashlib
import random
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

import pandas as pd

TABLE_SCHEMAS: Dict[str, Dict[str, Any]] = {'telco_customer_profile': {'businessName': 'Telecom customer master profile', 'description': 'Core customer profile used by CRM, billing, support, marketing consent, and KYC workflows for Egyptian telecom operators.', 'domain': 'telecom_crm', 'granularity': 'One row per natural person or business customer account holder.', 'columns': [{'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal customer key used across CRM and billing.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'full_name_en', 'businessName': 'Full legal name in English', 'logicalType': 'string', 'physicalType': 'VARCHAR(160)', 'description': 'Customer name written in Latin characters.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'full_name_ar', 'businessName': 'Full legal name in Arabic', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Customer name written in Arabic characters.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'primary_mobile', 'businessName': 'Primary Egyptian mobile number', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Primary contact number, normally normalized to E.164.', 'required': True, 'unique': True, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EGYPTIAN_MOBILE', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'alternate_phone', 'businessName': 'Alternate contact phone', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Secondary mobile or landline number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PHONE_NUMBER', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'email_address', 'businessName': 'Email address', 'logicalType': 'string', 'physicalType': 'VARCHAR(254)', 'description': 'Customer email used for notifications and account recovery.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EMAIL_ADDRESS', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'date_of_birth', 'businessName': 'Date of birth', 'logicalType': 'date', 'physicalType': 'DATE', 'description': 'Customer birth date.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'DATE_OF_BIRTH', 'is_pii': True, 'pii_category': 'quasi_identifier', 'sensitivity_level': 'high'}, {'name': 'gender', 'businessName': 'Gender', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Customer gender as collected in CRM/KYC.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'GENDER_INDICATOR', 'is_pii': True, 'pii_category': 'demographic', 'sensitivity_level': 'restricted'}, {'name': 'nationality', 'businessName': 'Nationality', 'logicalType': 'string', 'physicalType': 'CHAR(3)', 'description': 'ISO alpha-3 nationality code.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'NRP', 'is_pii': True, 'pii_category': 'sensitive_demographic', 'sensitivity_level': 'restricted'}, {'name': 'marketing_consent_flag', 'businessName': 'Marketing consent flag', 'logicalType': 'boolean', 'physicalType': 'BOOLEAN', 'description': 'Indicates whether customer opted in to promotional communication.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CONSENT_STATUS', 'is_pii': False, 'pii_category': 'privacy_preference', 'sensitivity_level': 'internal'}, {'name': 'created_at', 'businessName': 'Creation timestamp', 'logicalType': 'timestamp', 'physicalType': 'TIMESTAMP', 'description': 'Record creation timestamp.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'TIMESTAMP', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'telco_kyc_identity_document': {'businessName': 'Telecom KYC identity document registry', 'description': 'Stores identity-document attributes captured during SIM registration, account opening, or customer due diligence.', 'domain': 'telecom_kyc', 'granularity': 'One row per identity document submitted by a customer.', 'columns': [{'name': 'kyc_document_id', 'businessName': 'KYC document identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal KYC document key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Foreign key to telecom customer profile.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'eg_national_id', 'businessName': 'Egyptian national ID', 'logicalType': 'string', 'physicalType': 'CHAR(14)', 'description': '14-digit Egyptian National ID number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_NATIONAL_ID', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'passport_number', 'businessName': 'Passport number', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Passport number for Egyptian or non-Egyptian customers.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_PASSPORT', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'residency_permit_number', 'businessName': 'Residency permit number', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Permit number for non-Egyptian residents.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_RESIDENCY_PERMIT', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'driver_license_number', 'businessName': 'Driver license number', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Egyptian driver license or equivalent supporting ID.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_DRIVER_LICENSE', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'military_id', 'businessName': 'Military ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Military identity number where applicable.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_MILITARY_ID', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'syndicate_id', 'businessName': 'Professional syndicate ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(40)', 'description': 'Professional association card number such as doctors or engineers syndicate.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EG_SYNDICATE_ID', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'document_image_ref', 'businessName': 'Document image reference', 'logicalType': 'string', 'physicalType': 'VARCHAR(256)', 'description': 'Object-store reference to scanned identity image; do not store raw image in test data.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'BIOMETRIC_DATA', 'is_pii': True, 'pii_category': 'sensitive_document', 'sensitivity_level': 'critical'}, {'name': 'kyc_verified_at', 'businessName': 'KYC verification timestamp', 'logicalType': 'timestamp', 'physicalType': 'TIMESTAMP', 'description': 'Timestamp when KYC verification completed.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'TIMESTAMP', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'telco_subscriber_sim_registry': {'businessName': 'Subscriber and SIM registry', 'description': 'Represents active and historical SIM subscriptions, including Egyptian MSISDN, IMSI, ICCID, and operator-specific identifiers.', 'domain': 'telecom_bss', 'granularity': 'One row per SIM subscription lifecycle instance.', 'columns': [{'name': 'subscription_id', 'businessName': 'Subscription identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal subscription key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'SUBSCRIBER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Customer owning or using the subscription.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'msisdn', 'businessName': 'Mobile station ISDN number', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Customer-facing mobile number in Egyptian or E.164 format.', 'required': True, 'unique': True, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'MSISDN', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'operator_original_prefix', 'businessName': 'Original allocation operator', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Operator inferred from original Egyptian prefix; MNP may change actual operator.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CATEGORY', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'imsi', 'businessName': 'International Mobile Subscriber Identity', 'logicalType': 'string', 'physicalType': 'CHAR(15)', 'description': 'SIM subscriber identity used in mobile networks.', 'required': True, 'unique': True, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IMSI', 'is_pii': True, 'pii_category': 'telecom_identifier', 'sensitivity_level': 'critical'}, {'name': 'iccid', 'businessName': 'Integrated Circuit Card Identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(22)', 'description': 'SIM card serial number.', 'required': True, 'unique': True, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'ICCID', 'is_pii': True, 'pii_category': 'telecom_identifier', 'sensitivity_level': 'critical'}, {'name': 'sim_pin', 'businessName': 'SIM PIN', 'logicalType': 'string', 'physicalType': 'VARCHAR(8)', 'description': 'SIM PIN if captured in operational support context; should normally not be stored.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'PIN_CODE_SIM', 'is_pii': True, 'pii_category': 'credential', 'sensitivity_level': 'critical'}, {'name': 'puk_code', 'businessName': 'PUK code', 'logicalType': 'string', 'physicalType': 'CHAR(8)', 'description': 'PIN Unlock Key.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'PUK_CODE', 'is_pii': True, 'pii_category': 'credential', 'sensitivity_level': 'critical'}, {'name': 'activation_date', 'businessName': 'Activation date', 'logicalType': 'date', 'physicalType': 'DATE', 'description': 'SIM activation date.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'TIMESTAMP', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'status', 'businessName': 'Subscription status', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Active, suspended, terminated, or ported.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CATEGORY', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'telco_device_and_cpe_inventory': {'businessName': 'Customer device and CPE inventory', 'description': 'Tracks handset, router, ONT, and eSIM/device identifiers associated with subscribers and fixed broadband customers.', 'domain': 'telecom_oss', 'granularity': 'One row per customer-associated device or CPE asset.', 'columns': [{'name': 'device_asset_id', 'businessName': 'Device asset identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal asset key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Customer linked to the device.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'subscription_id', 'businessName': 'Subscription identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Subscription linked to the device.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'SUBSCRIBER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'imei', 'businessName': 'IMEI', 'logicalType': 'string', 'physicalType': 'CHAR(15)', 'description': 'Mobile equipment identifier; validate with Luhn where possible.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IMEI', 'is_pii': True, 'pii_category': 'device_identifier', 'sensitivity_level': 'critical'}, {'name': 'imeisv', 'businessName': 'IMEI software version', 'logicalType': 'string', 'physicalType': 'CHAR(16)', 'description': 'IMEI plus software version.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'IMEISV', 'is_pii': True, 'pii_category': 'device_identifier', 'sensitivity_level': 'high'}, {'name': 'embedded_sim_eid', 'businessName': 'eSIM EID', 'logicalType': 'string', 'physicalType': 'CHAR(32)', 'description': 'Embedded SIM identifier.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EMBEDDED_SIM_EID', 'is_pii': True, 'pii_category': 'telecom_identifier', 'sensitivity_level': 'critical'}, {'name': 'router_serial', 'businessName': 'Router serial number', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Customer premises router serial number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'ROUTER_SERIAL_ALPHANUM', 'is_pii': True, 'pii_category': 'device_identifier', 'sensitivity_level': 'high'}, {'name': 'ont_serial', 'businessName': 'ONT serial number', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'GPON ONT serial number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'ONT_SERIAL', 'is_pii': True, 'pii_category': 'device_identifier', 'sensitivity_level': 'high'}, {'name': 'mac_address', 'businessName': 'MAC address', 'logicalType': 'string', 'physicalType': 'VARCHAR(17)', 'description': 'Device MAC address.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'MAC_ADDRESS', 'is_pii': True, 'pii_category': 'network_identifier', 'sensitivity_level': 'high'}, {'name': 'installation_address_id', 'businessName': 'Installation address identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Reference to fixed-service installation address.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'LOCATION_ADDRESS', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}]}, 'telco_cdr_event': {'businessName': 'Call detail record event', 'description': 'Synthetic call/SMS/data event table for testing PII detection in CDR-style transactional datasets.', 'domain': 'telecom_network', 'granularity': 'One row per call, SMS, or data session event.', 'columns': [{'name': 'cdr_record_id', 'businessName': 'CDR record identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(40)', 'description': 'Unique event identifier from mediation or billing.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'CDR_RECORD_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'event_timestamp', 'businessName': 'Event timestamp', 'logicalType': 'timestamp', 'physicalType': 'TIMESTAMP', 'description': 'Start timestamp for the event.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'TIMESTAMP', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'a_party_msisdn', 'businessName': 'Calling party MSISDN', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Originating mobile number.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'MSISDN', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'b_party_msisdn', 'businessName': 'Called party MSISDN', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Terminating mobile number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'MSISDN', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'imsi', 'businessName': 'IMSI', 'logicalType': 'string', 'physicalType': 'CHAR(15)', 'description': 'Subscriber IMSI observed in event.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IMSI', 'is_pii': True, 'pii_category': 'telecom_identifier', 'sensitivity_level': 'critical'}, {'name': 'imei', 'businessName': 'IMEI', 'logicalType': 'string', 'physicalType': 'CHAR(15)', 'description': 'Device IMEI observed in event.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IMEI', 'is_pii': True, 'pii_category': 'device_identifier', 'sensitivity_level': 'critical'}, {'name': 'cell_global_identity', 'businessName': 'Cell global identity', 'logicalType': 'string', 'physicalType': 'VARCHAR(32)', 'description': 'Serving cell identifier, often linkable to approximate location.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'CELL_GLOBAL_IDENTITY', 'is_pii': True, 'pii_category': 'location_network_identifier', 'sensitivity_level': 'high'}, {'name': 'lac', 'businessName': 'Location area code', 'logicalType': 'integer', 'physicalType': 'INT', 'description': 'Network location area code.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'LAC', 'is_pii': True, 'pii_category': 'location_network_identifier', 'sensitivity_level': 'restricted'}, {'name': 'tac_lte', 'businessName': 'Tracking area code', 'logicalType': 'integer', 'physicalType': 'INT', 'description': 'LTE tracking area code.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'TAC_LTE', 'is_pii': True, 'pii_category': 'location_network_identifier', 'sensitivity_level': 'restricted'}, {'name': 'cgi_latitude', 'businessName': 'Cell latitude', 'logicalType': 'number', 'physicalType': 'DECIMAL(9,6)', 'description': 'Latitude of serving cell or enriched location.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'GPS_LATITUDE', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'cgi_longitude', 'businessName': 'Cell longitude', 'logicalType': 'number', 'physicalType': 'DECIMAL(9,6)', 'description': 'Longitude of serving cell or enriched location.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'GPS_LONGITUDE', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'source_ip', 'businessName': 'Source IP address', 'logicalType': 'string', 'physicalType': 'VARCHAR(45)', 'description': 'Subscriber IP address for data session.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'IP_ADDRESS', 'is_pii': True, 'pii_category': 'network_identifier', 'sensitivity_level': 'high'}, {'name': 'nat_public_ip', 'businessName': 'NAT public IP address', 'logicalType': 'string', 'physicalType': 'VARCHAR(45)', 'description': 'Carrier-grade NAT public IP.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'IPV4_CGNAT', 'is_pii': True, 'pii_category': 'network_identifier', 'sensitivity_level': 'high'}]}, 'telco_billing_invoice': {'businessName': 'Telecom billing and invoice table', 'description': 'Billing-account and invoice data for postpaid, prepaid hybrid, and fixed-line telecom services.', 'domain': 'telecom_billing', 'granularity': 'One row per issued invoice or billing cycle document.', 'columns': [{'name': 'invoice_id', 'businessName': 'Invoice identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal invoice key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'INVOICE_NUMBER', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'billing_account_number', 'businessName': 'Billing account number', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Customer billing account number.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'BILLING_ACCOUNT_NUMBER', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'high'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Customer billed by this invoice.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'bill_to_name', 'businessName': 'Bill-to customer name', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Name printed on invoice.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'bill_to_mobile', 'businessName': 'Bill-to mobile number', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Contact phone on invoice.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EGYPTIAN_MOBILE', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'billing_address', 'businessName': 'Billing address', 'logicalType': 'string', 'physicalType': 'NVARCHAR(300)', 'description': 'Full physical billing address.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'LOCATION_ADDRESS', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'tax_registration_number', 'businessName': 'Tax registration number', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Egyptian TRN for business customer invoices.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'EG_TAX_REGISTRATION_NUMBER', 'is_pii': True, 'pii_category': 'business_identifier', 'sensitivity_level': 'restricted'}, {'name': 'commercial_registry_number', 'businessName': 'Commercial registry number', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Egyptian CRN for business customers.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'EG_COMMERCIAL_REGISTRY_NUMBER', 'is_pii': True, 'pii_category': 'business_identifier', 'sensitivity_level': 'restricted'}, {'name': 'iban', 'businessName': 'Customer IBAN', 'logicalType': 'string', 'physicalType': 'VARCHAR(34)', 'description': 'IBAN used for bank transfer or refund.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IBAN_CODE', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'card_last4', 'businessName': 'Card last four digits', 'logicalType': 'string', 'physicalType': 'CHAR(4)', 'description': 'Masked payment-card suffix.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CREDIT_CARD', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'restricted'}, {'name': 'invoice_amount_egp', 'businessName': 'Invoice amount in EGP', 'logicalType': 'number', 'physicalType': 'DECIMAL(12,2)', 'description': 'Invoice amount.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'eshop_customer_account': {'businessName': 'E-commerce customer account', 'description': 'Customer account table for Egyptian e-commerce, marketplace, and digital retail use cases.', 'domain': 'ecommerce_crm', 'granularity': 'One row per registered customer account.', 'columns': [{'name': 'eshop_customer_id', 'businessName': 'E-shop customer ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal customer account key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'full_name', 'businessName': 'Full customer name', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Displayed or legal customer name.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'email_address', 'businessName': 'Email address', 'logicalType': 'string', 'physicalType': 'VARCHAR(254)', 'description': 'Login and notification email.', 'required': True, 'unique': True, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EMAIL_ADDRESS', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'mobile_number', 'businessName': 'Mobile number', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Egyptian mobile used for OTP, delivery, and support.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EGYPTIAN_MOBILE', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'password_hash', 'businessName': 'Password hash', 'logicalType': 'string', 'physicalType': 'VARCHAR(255)', 'description': 'Password hash; classify as secret even when not reversible.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'HASH_BCRYPT', 'is_pii': True, 'pii_category': 'credential', 'sensitivity_level': 'critical'}, {'name': 'session_id', 'businessName': 'Session identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(128)', 'description': 'Active or historical session identifier.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'SESSION_ID_ALPHANUM', 'is_pii': True, 'pii_category': 'credential', 'sensitivity_level': 'critical'}, {'name': 'csrf_token', 'businessName': 'CSRF token', 'logicalType': 'string', 'physicalType': 'VARCHAR(128)', 'description': 'Web anti-forgery token.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'CSRF_TOKEN', 'is_pii': True, 'pii_category': 'credential', 'sensitivity_level': 'critical'}, {'name': 'social_profile_url', 'businessName': 'Social login profile URL', 'logicalType': 'string', 'physicalType': 'VARCHAR(255)', 'description': 'Linked social profile URL where collected.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'FACEBOOK_PROFILE_URL', 'is_pii': True, 'pii_category': 'online_identifier', 'sensitivity_level': 'high'}, {'name': 'account_created_at', 'businessName': 'Account creation timestamp', 'logicalType': 'timestamp', 'physicalType': 'TIMESTAMP', 'description': 'Account creation timestamp.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'TIMESTAMP', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'eshop_shipping_address': {'businessName': 'E-commerce shipping address book', 'description': 'Address-book table containing Egyptian delivery-address components needed for e-commerce fulfillment.', 'domain': 'ecommerce_fulfillment', 'granularity': 'One row per saved customer shipping address.', 'columns': [{'name': 'address_id', 'businessName': 'Address identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal address key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'eshop_customer_id', 'businessName': 'E-shop customer ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Customer who owns this address.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'recipient_name', 'businessName': 'Recipient name', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Name of recipient at delivery location.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'recipient_mobile', 'businessName': 'Recipient mobile', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Mobile used by courier for delivery.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EGYPTIAN_MOBILE', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'governorate', 'businessName': 'Governorate', 'logicalType': 'string', 'physicalType': 'VARCHAR(60)', 'description': 'Egyptian governorate such as Cairo, Giza, Alexandria.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'GOVERNORATE', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'restricted'}, {'name': 'city', 'businessName': 'City', 'logicalType': 'string', 'physicalType': 'VARCHAR(80)', 'description': 'City or town.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'LOCATION_ADDRESS', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'restricted'}, {'name': 'district', 'businessName': 'District or neighborhood', 'logicalType': 'string', 'physicalType': 'NVARCHAR(120)', 'description': 'District, area, or neighborhood.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EG_DISTRICT', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'street_address', 'businessName': 'Street address', 'logicalType': 'string', 'physicalType': 'NVARCHAR(240)', 'description': 'Street name and house/building number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'LOCATION_ADDRESS', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'building_floor_apartment', 'businessName': 'Building floor and apartment', 'logicalType': 'string', 'physicalType': 'NVARCHAR(120)', 'description': 'Building, floor, apartment, landmark, or unit details.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EG_BUILDING_DETAILS', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'postal_code', 'businessName': 'Postal code', 'logicalType': 'string', 'physicalType': 'CHAR(5)', 'description': 'Egyptian five-digit postal code.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'EG_POSTAL_CODE', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'restricted'}, {'name': 'gps_pair', 'businessName': 'Delivery GPS coordinates', 'logicalType': 'string', 'physicalType': 'VARCHAR(50)', 'description': 'Optional GPS coordinates captured by courier or app.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'GPS_PAIR', 'is_pii': True, 'pii_category': 'precise_location', 'sensitivity_level': 'critical'}, {'name': 'what3words', 'businessName': 'What3Words address', 'logicalType': 'string', 'physicalType': 'VARCHAR(80)', 'description': 'Optional what3words precise address.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'WHAT3WORDS', 'is_pii': True, 'pii_category': 'precise_location', 'sensitivity_level': 'critical'}]}, 'eshop_order_header': {'businessName': 'E-commerce order header', 'description': 'Order-level table connecting customers, delivery address, payment channel, and fulfillment status.', 'domain': 'ecommerce_orders', 'granularity': 'One row per placed order.', 'columns': [{'name': 'order_id', 'businessName': 'Order identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Order key visible to customer and support.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'eshop_customer_id', 'businessName': 'E-shop customer ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Customer placing the order.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'recipient_name', 'businessName': 'Recipient name snapshot', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Recipient name copied from address at order time.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'recipient_mobile', 'businessName': 'Recipient mobile snapshot', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Delivery contact mobile.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EGYPTIAN_MOBILE', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'shipping_address_text', 'businessName': 'Shipping address snapshot', 'logicalType': 'string', 'physicalType': 'NVARCHAR(500)', 'description': 'Full delivery address at order time.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'LOCATION_ADDRESS', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'shipping_tracking_number', 'businessName': 'Shipping tracking number', 'logicalType': 'string', 'physicalType': 'VARCHAR(60)', 'description': 'Carrier tracking number shared with logistics provider.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'SHIPPING_TRACKING_NUMBER', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'payment_method', 'businessName': 'Payment method', 'logicalType': 'string', 'physicalType': 'VARCHAR(40)', 'description': 'Cash on delivery, card, Meeza, mobile wallet, Fawry, InstaPay, or BNPL.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CATEGORY', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'order_total_egp', 'businessName': 'Order total EGP', 'logicalType': 'number', 'physicalType': 'DECIMAL(12,2)', 'description': 'Total payable order amount.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'created_at', 'businessName': 'Order creation timestamp', 'logicalType': 'timestamp', 'physicalType': 'TIMESTAMP', 'description': 'Order creation timestamp.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'TIMESTAMP', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'fintech_payment_transaction': {'businessName': 'Egypt fintech payment transaction', 'description': 'Unified payment-transaction table covering card, Meeza, Fawry, mobile-wallet, InstaPay, and bank-transfer flows used by telecom and e-commerce.', 'domain': 'fintech_payments', 'granularity': 'One row per payment authorization, capture, transfer, or settlement transaction.', 'columns': [{'name': 'payment_transaction_id', 'businessName': 'Payment transaction ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(64)', 'description': 'Payment gateway transaction key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'STRIPE_PAYMENT_INTENT', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'restricted'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Customer associated with payment.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'payer_name', 'businessName': 'Payer name', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Name of payer when captured.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'payer_mobile', 'businessName': 'Payer mobile', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Mobile number linked to wallet or payer contact.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_MOBILE_WALLET_NUMBER', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'instapay_address', 'businessName': 'InstaPay address', 'logicalType': 'string', 'physicalType': 'VARCHAR(80)', 'description': 'InstaPay payment address or IPA.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'INSTAPAY_ADDRESS', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'fawry_reference', 'businessName': 'Fawry reference number', 'logicalType': 'string', 'physicalType': 'VARCHAR(40)', 'description': 'Fawry payment reference used for cash collection or bill payment.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_FAWRY_REFERENCE', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'meeza_card_pan', 'businessName': 'Meeza card PAN', 'logicalType': 'string', 'physicalType': 'VARCHAR(19)', 'description': 'Synthetic Meeza card number; never include real PANs in golden data.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_MEEZA_CARD', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'card_pan', 'businessName': 'Payment card PAN', 'logicalType': 'string', 'physicalType': 'VARCHAR(19)', 'description': 'Card primary account number; use tokenized or synthetic values only.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'CREDIT_CARD', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'card_expiry', 'businessName': 'Card expiry', 'logicalType': 'string', 'physicalType': 'VARCHAR(7)', 'description': 'Card expiration date.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'CARD_EXPIRY', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'cvv', 'businessName': 'Card verification value', 'logicalType': 'string', 'physicalType': 'VARCHAR(4)', 'description': 'Card CVV; should not be stored after authorization.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'CVV', 'is_pii': True, 'pii_category': 'credential_financial', 'sensitivity_level': 'critical'}, {'name': 'iban', 'businessName': 'IBAN', 'logicalType': 'string', 'physicalType': 'VARCHAR(34)', 'description': 'Bank account IBAN used for bank transfer or refund.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IBAN_CODE', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'bank_account_number', 'businessName': 'Local bank account number', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Egyptian or generic bank account number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_BANK_ACCOUNT', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'swift_bic', 'businessName': 'SWIFT/BIC', 'logicalType': 'string', 'physicalType': 'VARCHAR(11)', 'description': 'Bank routing identifier.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'SWIFT_BIC_EGYPT', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'restricted'}, {'name': 'amount_egp', 'businessName': 'Amount in EGP', 'logicalType': 'number', 'physicalType': 'DECIMAL(12,2)', 'description': 'Transaction amount.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'fintech_mobile_wallet_account': {'businessName': 'Egypt mobile wallet account', 'description': 'Mobile wallet account table for Vodafone Cash, Orange Cash, Etisalat Cash, WE Pay, and similar Egyptian wallet products.', 'domain': 'fintech_wallets', 'granularity': 'One row per mobile wallet account.', 'columns': [{'name': 'wallet_account_id', 'businessName': 'Wallet account ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal wallet account key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'wallet_msisdn', 'businessName': 'Wallet mobile number', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Mobile number that identifies the wallet account.', 'required': True, 'unique': True, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_MOBILE_WALLET_NUMBER', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Linked customer profile.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'eg_national_id', 'businessName': 'Egyptian national ID', 'logicalType': 'string', 'physicalType': 'CHAR(14)', 'description': 'KYC national ID linked to wallet.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_NATIONAL_ID', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'kyc_full_name', 'businessName': 'KYC full name', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Legal name verified for wallet.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'wallet_provider', 'businessName': 'Wallet provider', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Wallet brand or provider.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CATEGORY', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'wallet_pin_hash', 'businessName': 'Wallet PIN hash', 'logicalType': 'string', 'physicalType': 'VARCHAR(255)', 'description': 'Hash of wallet PIN if held by platform.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'HASH_BCRYPT', 'is_pii': True, 'pii_category': 'credential', 'sensitivity_level': 'critical'}, {'name': 'last_login_ip', 'businessName': 'Last login IP address', 'logicalType': 'string', 'physicalType': 'VARCHAR(45)', 'description': 'Last IP address used for wallet access.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'IP_ADDRESS', 'is_pii': True, 'pii_category': 'network_identifier', 'sensitivity_level': 'high'}, {'name': 'device_imei', 'businessName': 'Registered device IMEI', 'logicalType': 'string', 'physicalType': 'CHAR(15)', 'description': 'Device associated with wallet app.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IMEI', 'is_pii': True, 'pii_category': 'device_identifier', 'sensitivity_level': 'critical'}, {'name': 'status', 'businessName': 'Wallet status', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Active, suspended, closed, or restricted.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CATEGORY', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'merchant_seller_registry': {'businessName': 'Marketplace merchant and seller registry', 'description': 'Business seller registry for Egyptian e-commerce marketplaces, covering tax, commercial, settlement, and contact identifiers.', 'domain': 'ecommerce_merchant', 'granularity': 'One row per merchant legal entity or sole proprietor seller.', 'columns': [{'name': 'merchant_id', 'businessName': 'Merchant identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal seller or merchant key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'merchant_legal_name', 'businessName': 'Merchant legal name', 'logicalType': 'string', 'physicalType': 'NVARCHAR(200)', 'description': 'Registered business or sole proprietor legal name.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'merchant_contact_name', 'businessName': 'Merchant contact person', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Operational contact person.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'merchant_mobile', 'businessName': 'Merchant mobile', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Mobile number for merchant contact and OTP.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EGYPTIAN_MOBILE', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'merchant_email', 'businessName': 'Merchant email', 'logicalType': 'string', 'physicalType': 'VARCHAR(254)', 'description': 'Merchant contact email.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EMAIL_ADDRESS', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'tax_registration_number', 'businessName': 'Tax registration number', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Egyptian tax registration number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'EG_TAX_REGISTRATION_NUMBER', 'is_pii': True, 'pii_category': 'business_identifier', 'sensitivity_level': 'restricted'}, {'name': 'commercial_registry_number', 'businessName': 'Commercial registry number', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Egyptian commercial registry number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'EG_COMMERCIAL_REGISTRY_NUMBER', 'is_pii': True, 'pii_category': 'business_identifier', 'sensitivity_level': 'restricted'}, {'name': 'unified_national_number', 'businessName': 'Unified national number', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Unified establishment number where available.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'EG_UNIFIED_NATIONAL_NUMBER', 'is_pii': True, 'pii_category': 'business_identifier', 'sensitivity_level': 'restricted'}, {'name': 'settlement_iban', 'businessName': 'Settlement IBAN', 'logicalType': 'string', 'physicalType': 'VARCHAR(34)', 'description': 'Bank account IBAN for merchant settlement.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IBAN_CODE', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'business_address', 'businessName': 'Business address', 'logicalType': 'string', 'physicalType': 'NVARCHAR(300)', 'description': 'Registered business address.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'LOCATION_ADDRESS', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'national_real_estate_id', 'businessName': 'National real estate ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(40)', 'description': 'Property identifier for business premises where available.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'EG_NATIONAL_REAL_ESTATE_ID', 'is_pii': True, 'pii_category': 'location_identifier', 'sensitivity_level': 'restricted'}]}, 'support_ticket_free_text': {'businessName': 'Support ticket and free-text interaction log', 'description': 'Free-text ticket data for evaluating entity extraction from notes, SMS bodies, chat transcripts, emails, and logs.', 'domain': 'cross_domain_support', 'granularity': 'One row per support interaction or message.', 'columns': [{'name': 'ticket_id', 'businessName': 'Ticket identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Support ticket identifier.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Customer linked to ticket.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'channel', 'businessName': 'Interaction channel', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Call center, chat, email, WhatsApp, SMS, app, or branch.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CATEGORY', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'agent_notes', 'businessName': 'Agent free-text notes', 'logicalType': 'string', 'physicalType': 'NVARCHAR(4000)', 'description': 'Free text may contain phone, national ID, email, address, OTP, card, IBAN, IMEI, IMSI, ICCID, or passport values.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'FREE_TEXT', 'is_pii': True, 'pii_category': 'mixed_unstructured_pii', 'sensitivity_level': 'critical'}, {'name': 'sms_body', 'businessName': 'SMS body', 'logicalType': 'string', 'physicalType': 'NVARCHAR(500)', 'description': 'SMS content for OTP and notification detection.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'FREE_TEXT', 'is_pii': True, 'pii_category': 'mixed_unstructured_pii', 'sensitivity_level': 'critical'}, {'name': 'attachment_text', 'businessName': 'Extracted attachment text', 'logicalType': 'string', 'physicalType': 'NVARCHAR(4000)', 'description': 'OCR or extracted attachment text, often containing ID documents or invoices.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'FREE_TEXT', 'is_pii': True, 'pii_category': 'mixed_unstructured_pii', 'sensitivity_level': 'critical'}, {'name': 'detected_language', 'businessName': 'Detected language', 'logicalType': 'string', 'physicalType': 'VARCHAR(10)', 'description': 'Language code for text-processing evaluation.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CATEGORY', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'created_at', 'businessName': 'Ticket creation timestamp', 'logicalType': 'timestamp', 'physicalType': 'TIMESTAMP', 'description': 'Ticket timestamp.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'TIMESTAMP', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'sensitive_customer_risk_profile': {'businessName': 'Sensitive customer risk and eligibility profile', 'description': 'Synthetic table for high-risk sensitive personal data classes under privacy and sector controls. Use only synthetic values.', 'domain': 'cross_domain_sensitive', 'granularity': 'One row per customer risk or eligibility assessment snapshot.', 'columns': [{'name': 'risk_profile_id', 'businessName': 'Risk profile ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal risk profile key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Linked customer.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'criminal_record_reference', 'businessName': 'Criminal record reference', 'logicalType': 'string', 'physicalType': 'VARCHAR(60)', 'description': 'Police record or criminal-status reference where legally collected.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_CRIMINAL_RECORD', 'is_pii': True, 'pii_category': 'sensitive_personal_data', 'sensitivity_level': 'critical'}, {'name': 'health_insurance_id', 'businessName': 'Health insurance identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Health insurance or medical benefit member ID.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'HEALTH_DATA', 'is_pii': True, 'pii_category': 'sensitive_personal_data', 'sensitivity_level': 'critical'}, {'name': 'medical_record_number', 'businessName': 'Medical record number', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Medical record number in partner add-on services.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'MEDICAL_RECORD_NUMBER', 'is_pii': True, 'pii_category': 'sensitive_personal_data', 'sensitivity_level': 'critical'}, {'name': 'biometric_template_ref', 'businessName': 'Biometric template reference', 'logicalType': 'string', 'physicalType': 'VARCHAR(256)', 'description': 'Reference to biometric template, fingerprint, face, or liveness artifact.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'BIOMETRIC_DATA', 'is_pii': True, 'pii_category': 'sensitive_personal_data', 'sensitivity_level': 'critical'}, {'name': 'religion', 'businessName': 'Religion', 'logicalType': 'string', 'physicalType': 'VARCHAR(50)', 'description': 'Religion where collected for lawful personal-status context; avoid unless necessary.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'RELIGION', 'is_pii': True, 'pii_category': 'sensitive_personal_data', 'sensitivity_level': 'critical'}, {'name': 'political_affiliation', 'businessName': 'Political affiliation', 'logicalType': 'string', 'physicalType': 'VARCHAR(100)', 'description': 'Political view or affiliation if present in unstructured/third-party data.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'NRP', 'is_pii': True, 'pii_category': 'sensitive_personal_data', 'sensitivity_level': 'critical'}, {'name': 'children_data_flag', 'businessName': 'Children data indicator', 'logicalType': 'boolean', 'physicalType': 'BOOLEAN', 'description': 'Indicates whether data subject is a child/minor.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'CHILDREN_DATA', 'is_pii': True, 'pii_category': 'sensitive_personal_data', 'sensitivity_level': 'critical'}]}}

FIRST_NAMES_EN = ["Ahmed", "Mohamed", "Mahmoud", "Omar", "Youssef", "Mariam", "Nour", "Salma", "Hana", "Farida"]
FATHER_NAMES_EN = ["Hassan", "Ali", "Ibrahim", "Mostafa", "Samir", "Khaled", "Tarek", "Adel", "Nabil", "Fouad"]
FAMILY_NAMES_EN = ["ElSayed", "Abdelrahman", "Mahfouz", "Shoukry", "Fathy", "Gaber", "Zaki", "Kamel", "Nassar", "Darwish"]
FIRST_NAMES_AR = ["أحمد", "محمد", "محمود", "عمر", "يوسف", "مريم", "نور", "سلمى", "هنا", "فريدة"]
FATHER_NAMES_AR = ["حسن", "علي", "إبراهيم", "مصطفى", "سمير", "خالد", "طارق", "عادل", "نبيل", "فؤاد"]
FAMILY_NAMES_AR = ["السيد", "عبد الرحمن", "محفوظ", "شكري", "فتحي", "جابر", "زكي", "كامل", "نصار", "درويش"]
GOVERNORATES = ["Cairo", "Giza", "Alexandria", "Dakahlia", "Red Sea", "Beheira", "Fayoum", "Gharbia", "Ismailia", "Menofia", "Minya", "Qaliubiya", "New Valley", "Suez", "Aswan", "Assiut", "Beni Suef", "Port Said", "Damietta", "Sharkia", "South Sinai", "Kafr El Sheikh", "Matrouh", "Luxor", "Qena", "North Sinai", "Sohag"]
DISTRICTS = ["Nasr City", "Maadi", "Heliopolis", "Dokki", "Mohandessin", "6th of October", "Smouha", "Mansoura", "Tanta", "Zamalek"]
STREETS = ["Tahrir Street", "El Nasr Road", "Gameat El Dowal", "Corniche El Nile", "Makram Ebeid", "Salah Salem", "Abbas El Akkad", "Port Said Street"]
EMAIL_DOMAINS = ["example.eg", "mail.test", "customer.invalid", "synthetic.local"]

TABLE_PREFIXES = {
    "telco_customer_profile": "CUST",
    "telco_kyc_identity_document": "KYC",
    "telco_sim_subscription": "SIM",
    "telco_device_inventory": "DEV",
    "telco_cdr_event": "CDR",
    "telco_network_location_event": "NLE",
    "eshop_customer_account": "ESH-CUST",
    "eshop_order_header": "ORD",
    "eshop_shipping_address": "ADDR",
    "eshop_merchant_profile": "MER",
    "fintech_wallet_account": "WAL",
    "fintech_payment_transaction": "PAY",
    "customer_support_interaction": "SUP",
    "sensitive_special_category_record": "SEN",
}


def _slug_name(i: int) -> str:
    return f"{FIRST_NAMES_EN[i % len(FIRST_NAMES_EN)].lower()}.{FAMILY_NAMES_EN[(i * 7) % len(FAMILY_NAMES_EN)].lower()}"


def _full_name_en(i: int) -> str:
    return f"{FIRST_NAMES_EN[i % len(FIRST_NAMES_EN)]} {FATHER_NAMES_EN[(i * 3) % len(FATHER_NAMES_EN)]} {FAMILY_NAMES_EN[(i * 7) % len(FAMILY_NAMES_EN)]}"


def _full_name_ar(i: int) -> str:
    return f"{FIRST_NAMES_AR[i % len(FIRST_NAMES_AR)]} {FATHER_NAMES_AR[(i * 3) % len(FATHER_NAMES_AR)]} {FAMILY_NAMES_AR[(i * 7) % len(FAMILY_NAMES_AR)]}"


def _egypt_mobile(i: int, e164: bool = True) -> str:
    prefixes = ["010", "011", "012", "015"]
    local10 = prefixes[i % len(prefixes)] + f"{10000000 + (i * 7919) % 90000000:08d}"
    return "+20" + local10[1:] if e164 else local10


def _landline(i: int) -> str:
    areas = ["02", "03", "040", "050", "055", "064", "088", "097"]
    area = areas[i % len(areas)]
    digits_needed = 10 - len(area)
    return area + f"{1000000 + (i * 3571) % 9000000:0{digits_needed}d}"[-digits_needed:]


def _email(i: int) -> str:
    return f"{_slug_name(i)}{i:04d}@{EMAIL_DOMAINS[i % len(EMAIL_DOMAINS)]}"


def _birth_date(i: int) -> date:
    return date(1965 + (i % 42), 1 + (i * 7) % 12, 1 + (i * 13) % 28)


def _egypt_national_id(i: int) -> str:
    dob = _birth_date(i)
    century = "2" if dob.year < 2000 else "3"
    yy = dob.year % 100
    gov_codes = ["01", "02", "03", "04", "11", "12", "13", "14", "15", "16", "17", "18", "19", "21", "22", "23", "24", "25", "26", "27", "28", "29", "31", "32", "33", "34", "35", "88"]
    gov = gov_codes[i % len(gov_codes)]
    seq = f"{1000 + (i * 17) % 9000:04d}"
    check = str((i * 7 + 3) % 10)
    return f"{century}{yy:02d}{dob.month:02d}{dob.day:02d}{gov}{seq}{check}"


def _imei(i: int) -> str:
    return f"49{(154203237000 + i) % 10**13:013d}"[-15:]


def _imsi(i: int) -> str:
    return "602" + ["01", "02", "03", "04"][i % 4] + f"{1000000000 + i:010d}"[-10:]


def _iccid(i: int) -> str:
    return "8920" + f"{1000000000000000 + i:016d}"


def _address(i: int) -> str:
    return f"{12 + i % 80} {STREETS[i % len(STREETS)]}, {DISTRICTS[i % len(DISTRICTS)]}, {GOVERNORATES[i % len(GOVERNORATES)]}"


def _geo(i: int) -> str:
    lat = 30.0444 + ((i % 1000) - 500) / 10000
    lon = 31.2357 + (((i * 3) % 1000) - 500) / 10000
    return f"{lat:.6f}, {lon:.6f}"


def _iban(i: int) -> str:
    return "EG" + f"{38 + i % 61:02d}" + f"{19 + i % 80:04d}" + f"{5000000000263180000 + i:019d}"[:23]


def _crypto(i: int) -> str:
    prefix = ["bc1q", "0x", "T"][i % 3]
    return prefix + hashlib.sha256(f"synthetic-secret-{i}".encode()).hexdigest()


def _timestamp(i: int) -> pd.Timestamp:
    return pd.Timestamp(datetime(2026, 1, 1, 8, 0, 0) + timedelta(minutes=i * 7))


def _semantic_value(table_name: str, column: Dict[str, Any], i: int, rng: random.Random) -> Any:
    """Create a deterministic synthetic value for one schema column."""
    name = column["name"].lower()
    sem = str(column.get("semantic_type") or "").upper()
    logical = str(column.get("logicalType") or "string").lower()

    if column.get("primaryKey") or name.endswith("_id"):
        prefix = TABLE_PREFIXES.get(table_name, "ID")
        return f"{prefix}-{i+1:08d}"
    if sem == "PERSON" or "full_name_en" in name:
        return _full_name_ar(i) if name.endswith("_ar") else _full_name_en(i)
    if name.endswith("_ar") or "arabic" in name:
        return _full_name_ar(i)
    if sem in {"EGYPTIAN_MOBILE", "MSISDN"} or "mobile" in name or "msisdn" in name:
        return _egypt_mobile(i, e164=(sem == "MSISDN" or "e164" in name or "primary" in name))
    if sem == "PHONE_NUMBER" or "phone" in name:
        return _landline(i) if i % 3 == 0 else _egypt_mobile(i, e164=False)
    if sem == "EMAIL_ADDRESS" or "email" in name:
        return _email(i)
    if sem == "EG_NATIONAL_ID" or "national_id" in name:
        return _egypt_national_id(i)
    if sem == "DATE_OF_BIRTH" or "birth" in name:
        return _birth_date(i)
    if sem == "EG_PASSPORT" or "passport" in name:
        return chr(65 + i % 26) + f"{10000000 + i:08d}"[-8:]
    if sem in {"EG_DRIVER_LICENSE", "DOCUMENT_NUMBER"} or "document_number" in name or "license" in name:
        return f"{sem.split('_')[-2][:3] if '_' in sem else 'DOC'}-{100000 + i}"
    if sem == "DOCUMENT_IMAGE_URI" or "image" in name or "artifact" in name:
        return f"s3://synthetic-pii-artifacts/{table_name}/{i+1:08d}.bin"
    if sem == "IMEI" or "imei" in name:
        return _imei(i)
    if sem == "IMSI" or "imsi" in name:
        return _imsi(i)
    if sem == "ICCID" or "iccid" in name:
        return _iccid(i)
    if sem == "MAC_ADDRESS" or "mac" in name:
        return ":".join(f"{(i * k + k) % 256:02x}" for k in range(1, 7))
    if sem == "IP_ADDRESS" or name in {"ip", "ip_address", "source_ip"}:
        return f"10.{i % 255}.{(i * 3) % 255}.{(i * 7) % 255}"
    if sem == "OTP" or "otp" in name:
        return f"{100000 + i % 900000:06d}"
    if sem == "CVV" or "cvv" in name:
        return f"{100 + i % 900:03d}"
    if sem == "CARD_EXPIRY" or "expiry" in name:
        return f"{1 + i % 12:02d}/{26 + i % 8}"
    if sem in {"CREDIT_CARD", "PAYMENT_CARD_TOKEN"} or "card" in name:
        return "627033" + f"{1000000000 + i:010d}" if "meeza" in name else "411111" + f"{1000000000 + i:010d}"
    if sem == "IBAN_CODE" or "iban" in name:
        return _iban(i)
    if sem == "EG_BANK_ACCOUNT" or "bank_account" in name:
        return f"{100000000000 + i:012d}"
    if sem == "INSTAPAY_ADDRESS" or "instapay" in name:
        return f"{_slug_name(i)}{i:03d}@instapay"
    if sem == "FAWRY_REFERENCE" or "fawry" in name:
        return f"FWY{202600000000 + i}"
    if sem == "CRYPTO" or "wallet_address" in name:
        return _crypto(i)
    if sem in {"LOCATION_ADDRESS", "SHIPPING_ADDRESS"} or "address" in name:
        return _address(i)
    if sem == "GOVERNORATE" or "governorate" in name:
        return GOVERNORATES[i % len(GOVERNORATES)]
    if sem in {"GEOLOCATION", "GPS_COORDINATES"} or "latitude" in name or "longitude" in name or "geo" in name:
        if "latitude" in name:
            return round(30.0444 + ((i % 1000) - 500) / 10000, 6)
        if "longitude" in name:
            return round(31.2357 + (((i * 3) % 1000) - 500) / 10000, 6)
        return _geo(i)
    if sem == "CELL_GLOBAL_IDENTITY" or "cell" in name:
        return f"602-{1 + i % 4:02d}-{100 + i % 60000}-{1000 + i % 50000}" if sem == "CELL_GLOBAL_IDENTITY" else str(1000 + i % 60000)
    if sem == "COOKIE_ID" or "cookie" in name:
        return hashlib.md5(f"cookie-{table_name}-{i}".encode()).hexdigest()
    if sem == "DEVICE_ADVERTISING_ID" or "advertising" in name:
        return hashlib.sha1(f"adid-{i}".encode()).hexdigest()[:32]
    if sem == "SOCIAL_MEDIA_HANDLE" or "social" in name:
        return f"https://facebook.com/{_slug_name(i)}"
    if sem == "TAX_ID" or "tax" in name:
        return f"{100000000 + i:09d}"
    if sem == "COMMERCIAL_REGISTRATION" or "commercial" in name:
        return f"CR-{100000 + i}"
    if sem == "TRACKING_NUMBER" or "tracking" in name:
        return f"EG-TRK-{2026000000 + i}"
    if sem == "FREE_TEXT" or "notes" in name or "message" in name or "description" in name or "transcript" in name:
        if "otp" in name or i % 5 == 0:
            return f"Your OTP is {100000 + i % 900000} for login to your account. Do not share it."
        return f"Customer {_egypt_mobile(i)} confirmed NID {_egypt_national_id(i)} and requested update for IMEI {_imei(i)}."
    if sem in {"HEALTH_DATA", "MEDICAL_RECORD_NUMBER"} or "medical" in name or "health" in name:
        return f"MRN-{100000 + i}"
    if sem in {"RELIGION", "POLITICAL_OPINION", "CHILDREN_DATA", "BIOMETRIC_IDENTIFIER"}:
        values = {
            "RELIGION": ["Muslim", "Christian", "Prefer not to say"],
            "POLITICAL_OPINION": ["Not collected", "Union member", "Campaign preference"],
            "CHILDREN_DATA": ["guardian_verified_minor", "school_delivery_contact", "child_profile_flag"],
            "BIOMETRIC_IDENTIFIER": ["face_template_hash_", "voiceprint_hash_", "fingerprint_hash_"],
        }
        base = values[sem][i % len(values[sem])]
        return base + (hashlib.sha256(str(i).encode()).hexdigest()[:12] if base.endswith("_") else "")
    if sem == "PCR_TEST_ID" or "pcr" in name:
        return f"PCR-{100000 + i}"
    if sem == "NRP" or "nationality" in name:
        return ["EGY", "SYR", "SDN", "JOR", "PSE"][i % 5]
    if "gender" in name:
        return ["M", "F", "Unspecified"][i % 3]
    if "status" in name:
        return ["active", "pending", "suspended", "closed"][i % 4]
    if "type" in name or "category" in name:
        return ["prepaid", "postpaid", "wallet", "card", "cash_on_delivery"][i % 5]
    if "amount" in name or logical in {"decimal", "double", "float"}:
        return round(25 + (i * 13.75) % 25000, 2)
    if logical in {"integer", "int", "long", "bigint"} or name.endswith("_count"):
        return int(1 + (i * 17) % 100000)
    if logical == "boolean" or name.endswith("_flag") or name.startswith("is_"):
        return bool(i % 2)
    if logical == "date":
        return date(2025 + (i % 2), 1 + (i % 12), 1 + (i % 28))
    if logical == "timestamp" or name.endswith("_at") or "timestamp" in name or "time" in name:
        return _timestamp(i)
    return f"{name}_{i+1:08d}"


def _expected_columns(table_name: str) -> List[str]:
    if table_name not in TABLE_SCHEMAS:
        raise KeyError(f"Unknown table_name={table_name!r}. Expected one of: {sorted(TABLE_SCHEMAS)}")
    return [c["name"] for c in TABLE_SCHEMAS[table_name]["columns"]]


def _coerce_dataframe(table_name: str, df: pd.DataFrame, validate_required: bool = True, strict_columns: bool = False, add_missing_columns: bool = True) -> pd.DataFrame:
    table = TABLE_SCHEMAS[table_name]
    expected = [c["name"] for c in table["columns"]]
    lower_to_actual = {str(col).lower(): col for col in df.columns}
    renamed = df.rename(columns={actual: expected_name for expected_name in expected for lower, actual in lower_to_actual.items() if lower == expected_name.lower()})

    missing = [c for c in expected if c not in renamed.columns]
    if validate_required:
        required_missing = [c["name"] for c in table["columns"] if c.get("required") and c["name"] not in renamed.columns]
        if required_missing:
            raise ValueError(f"CSV is missing required columns for table {table_name!r}: {required_missing}")
    if missing and not add_missing_columns:
        raise ValueError(f"CSV is missing expected columns for table {table_name!r}: {missing}")
    for col in missing:
        renamed[col] = pd.NA

    extra = [c for c in renamed.columns if c not in expected]
    if strict_columns and extra:
        raise ValueError(f"CSV contains unexpected columns for table {table_name!r}: {extra}")

    result = renamed[expected].copy()
    for col_meta in table["columns"]:
        col = col_meta["name"]
        logical = str(col_meta.get("logicalType") or "string").lower()
        if logical == "date":
            result[col] = pd.to_datetime(result[col], errors="coerce").dt.date
        elif logical == "timestamp":
            result[col] = pd.to_datetime(result[col], errors="coerce")
        elif logical == "boolean":
            result[col] = result[col].map(lambda x: pd.NA if pd.isna(x) else str(x).strip().lower() in {"true", "1", "yes", "y"})
        elif logical in {"integer", "int", "long", "bigint"}:
            result[col] = pd.to_numeric(result[col], errors="coerce").astype("Int64")
        elif logical in {"decimal", "double", "float"}:
            result[col] = pd.to_numeric(result[col], errors="coerce")
        else:
            result[col] = result[col].astype("string")
    return result


def _write_dataframe(df: pd.DataFrame, target_path: Union[str, Path], mode: str = "append", output_format: Optional[str] = None, index: bool = False) -> None:
    path = Path(target_path)
    fmt = (output_format or path.suffix.lstrip(".") or "csv").lower()
    if mode not in {"append", "overwrite", "errorifexists"}:
        raise ValueError("mode must be one of: 'append', 'overwrite', 'errorifexists'")
    if mode == "errorifexists" and path.exists():
        raise FileExistsError(f"Target path already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)

    if mode == "append" and path.exists():
        if fmt == "csv":
            existing = pd.read_csv(path)
        elif fmt == "parquet":
            existing = pd.read_parquet(path)
        elif fmt in {"json", "jsonl"}:
            existing = pd.read_json(path, lines=(fmt == "jsonl"))
        else:
            raise ValueError(f"Unsupported output format: {fmt}")
        df = pd.concat([existing, df], ignore_index=True)

    if fmt == "csv":
        df.to_csv(path, index=index)
    elif fmt == "parquet":
        df.to_parquet(path, index=index)
    elif fmt == "json":
        df.to_json(path, orient="records", force_ascii=False, indent=2)
    elif fmt == "jsonl":
        df.to_json(path, orient="records", lines=True, force_ascii=False)
    else:
        raise ValueError(f"Unsupported output format: {fmt}")


def generate_table(table_name: str, num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate synthetic records for any configured table by name.

    Inputs:
        table_name: Exact table schema name, such as ``telco_customer_profile``.
        num_rows: Number of synthetic rows to create. Must be zero or positive.
        seed: Deterministic random seed reserved for future stochastic variants.

    Returns:
        A pandas DataFrame ordered according to the configured table schema.

    Example:
        >>> df = generate_table("telco_cdr_event", 1000, seed=42)
        >>> df.head()

    Agent-use hint:
        Use this dispatcher when the table name is selected dynamically by a
        workflow, benchmark runner, or natural-language agent instruction.
    """
    if table_name not in TABLE_SCHEMAS:
        raise KeyError(f"Unknown table_name={table_name!r}. Expected one of: {sorted(TABLE_SCHEMAS)}")
    if num_rows < 0:
        raise ValueError("num_rows must be zero or positive")
    rng = random.Random(seed)
    table = TABLE_SCHEMAS[table_name]
    rows = []
    for i in range(num_rows):
        rows.append({c["name"]: _semantic_value(table_name, c, i, rng) for c in table["columns"]})
    return pd.DataFrame(rows, columns=_expected_columns(table_name))


def load_table_csv(
    table_name: str,
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records for any configured table, validate shape, and optionally persist.

    Inputs:
        table_name: Exact table schema name.
        csv_path: Local or mounted CSV path accepted by ``pandas.read_csv``.
        existing_df: Optional DataFrame to concatenate before returning. This is
            useful when an agent has an in-memory table and wants to add records.
        target_path: Optional output file. If provided, the resulting DataFrame
            is written to CSV, Parquet, JSON, or JSONL depending on extension or
            ``output_format``.
        mode: ``append``, ``overwrite``, or ``errorifexists`` when writing to
            ``target_path``. For append mode, an existing target file is read and
            concatenated before writing.
        output_format: Optional explicit format: ``csv``, ``parquet``, ``json``,
            or ``jsonl``.
        validate_required: Raise an error if required schema columns are absent.
        strict_columns: Raise an error for unexpected columns.
        add_missing_columns: Add nullable missing schema columns as ``pd.NA``.
        read_csv_kwargs: Optional dictionary forwarded to ``pandas.read_csv``.

    Returns:
        A pandas DataFrame containing schema-ordered, coerced records.

    Example:
        >>> df = load_table_csv("eshop_order_header", "orders.csv", target_path="golden_orders.csv")

    Agent-use hint:
        For user-uploaded files, keep ``validate_required=True``. Use
        ``strict_columns=True`` only when the agent is checking exact schema
        compliance instead of allowing harmless source-system extra columns.
    """
    if table_name not in TABLE_SCHEMAS:
        raise KeyError(f"Unknown table_name={table_name!r}. Expected one of: {sorted(TABLE_SCHEMAS)}")
    kwargs = read_csv_kwargs or {}
    raw = pd.read_csv(csv_path, **kwargs)
    loaded = _coerce_dataframe(table_name, raw, validate_required=validate_required, strict_columns=strict_columns, add_missing_columns=add_missing_columns)
    if existing_df is not None:
        existing = _coerce_dataframe(table_name, existing_df, validate_required=False, strict_columns=False, add_missing_columns=True)
        loaded = pd.concat([existing, loaded], ignore_index=True)
    if target_path is not None:
        _write_dataframe(loaded, target_path=target_path, mode=mode, output_format=output_format)
    return loaded


def get_table_columns(table_name: str) -> List[str]:
    """Return the expected column order for one configured table."""
    return _expected_columns(table_name)


def describe_table(table_name: str) -> Dict[str, Any]:
    """Return a deep copy of table metadata, including column PII semantic labels."""
    if table_name not in TABLE_SCHEMAS:
        raise KeyError(f"Unknown table_name={table_name!r}. Expected one of: {sorted(TABLE_SCHEMAS)}")
    return deepcopy(TABLE_SCHEMAS[table_name])


def list_tables() -> List[str]:
    """Return all configured table names in sorted order."""
    return sorted(TABLE_SCHEMAS)


def pii_columns(table_name: str) -> List[Dict[str, Any]]:
    """Return metadata for columns marked as PII in one table."""
    return [deepcopy(c) for c in describe_table(table_name)["columns"] if c.get("is_pii")]



def generate_telco_customer_profile(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic pandas DataFrame for ``telco_customer_profile``.

    Table definition:
        Telecom customer master profile. Core customer profile used by CRM, billing, support, marketing consent, and KYC workflows for Egyptian telecom operators.

    Inputs:
        num_rows: Number of synthetic rows to generate. Use a small value such
            as 10 for unit tests and a larger value such as 10_000 for benchmark
            runs. Must be zero or positive.
        seed: Deterministic seed. The current generator is mostly deterministic
            by row index, but the seed is included for repeatable agent workflows
            and future stochastic variants.

    Returns:
        pandas.DataFrame with the exact column order defined for ``telco_customer_profile``.

    Example:
        >>> df = generate_telco_customer_profile(num_rows=100, seed=42)
        >>> df.columns.tolist() == get_table_columns("telco_customer_profile")
        True

    Agent-use hint:
        Call this function when an agent needs realistic synthetic golden data
        for the ``telco_customer_profile`` scenario before passing the DataFrame to a PII
        scanner, anonymizer, profiler, classifier, or data-quality test.
    """
    return generate_table("telco_customer_profile", num_rows=num_rows, seed=seed)


def load_telco_customer_profile_csv(
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records and add them to the pandas representation of ``telco_customer_profile``.

    Table definition:
        Telecom customer master profile. Core customer profile used by CRM, billing, support, marketing consent, and KYC workflows for Egyptian telecom operators.

    Inputs:
        csv_path: CSV file path containing rows to load for this table.
        existing_df: Optional existing pandas DataFrame. If provided, the loaded
            CSV rows are schema-normalized and appended to this DataFrame.
        target_path: Optional file path where the combined result should be
            written. The extension or ``output_format`` controls CSV, Parquet,
            JSON, or JSONL output.
        mode: Write behavior for ``target_path``. Use ``append`` to add records
            to an existing file, ``overwrite`` to replace it, or ``errorifexists``
            for defensive agent workflows.
        output_format: Optional explicit output format: ``csv``, ``parquet``,
            ``json``, or ``jsonl``.
        validate_required: When true, missing required columns raise an error.
        strict_columns: When true, unexpected source columns raise an error.
        add_missing_columns: When true, missing optional columns are added as
            ``pd.NA`` to preserve the schema contract.
        read_csv_kwargs: Optional dictionary passed to ``pandas.read_csv``.

    Returns:
        pandas.DataFrame containing schema-ordered, type-coerced records for
        ``telco_customer_profile``.

    Example:
        >>> df = load_telco_customer_profile_csv("telco_customer_profile.csv", target_path="out/telco_customer_profile.csv", mode="overwrite")

    Agent-use hint:
        Use this function after a user uploads CSV records for ``telco_customer_profile``.
        Keep ``validate_required=True`` to catch incomplete input. Enable
        ``strict_columns=True`` only when the benchmark should fail files with
        extra source columns.
    """
    return load_table_csv(
        "telco_customer_profile",
        csv_path=csv_path,
        existing_df=existing_df,
        target_path=target_path,
        mode=mode,
        output_format=output_format,
        validate_required=validate_required,
        strict_columns=strict_columns,
        add_missing_columns=add_missing_columns,
        read_csv_kwargs=read_csv_kwargs,
    )



def generate_telco_kyc_identity_document(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic pandas DataFrame for ``telco_kyc_identity_document``.

    Table definition:
        Telecom KYC identity document registry. Stores identity-document attributes captured during SIM registration, account opening, or customer due diligence.

    Inputs:
        num_rows: Number of synthetic rows to generate. Use a small value such
            as 10 for unit tests and a larger value such as 10_000 for benchmark
            runs. Must be zero or positive.
        seed: Deterministic seed. The current generator is mostly deterministic
            by row index, but the seed is included for repeatable agent workflows
            and future stochastic variants.

    Returns:
        pandas.DataFrame with the exact column order defined for ``telco_kyc_identity_document``.

    Example:
        >>> df = generate_telco_kyc_identity_document(num_rows=100, seed=42)
        >>> df.columns.tolist() == get_table_columns("telco_kyc_identity_document")
        True

    Agent-use hint:
        Call this function when an agent needs realistic synthetic golden data
        for the ``telco_kyc_identity_document`` scenario before passing the DataFrame to a PII
        scanner, anonymizer, profiler, classifier, or data-quality test.
    """
    return generate_table("telco_kyc_identity_document", num_rows=num_rows, seed=seed)


def load_telco_kyc_identity_document_csv(
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records and add them to the pandas representation of ``telco_kyc_identity_document``.

    Table definition:
        Telecom KYC identity document registry. Stores identity-document attributes captured during SIM registration, account opening, or customer due diligence.

    Inputs:
        csv_path: CSV file path containing rows to load for this table.
        existing_df: Optional existing pandas DataFrame. If provided, the loaded
            CSV rows are schema-normalized and appended to this DataFrame.
        target_path: Optional file path where the combined result should be
            written. The extension or ``output_format`` controls CSV, Parquet,
            JSON, or JSONL output.
        mode: Write behavior for ``target_path``. Use ``append`` to add records
            to an existing file, ``overwrite`` to replace it, or ``errorifexists``
            for defensive agent workflows.
        output_format: Optional explicit output format: ``csv``, ``parquet``,
            ``json``, or ``jsonl``.
        validate_required: When true, missing required columns raise an error.
        strict_columns: When true, unexpected source columns raise an error.
        add_missing_columns: When true, missing optional columns are added as
            ``pd.NA`` to preserve the schema contract.
        read_csv_kwargs: Optional dictionary passed to ``pandas.read_csv``.

    Returns:
        pandas.DataFrame containing schema-ordered, type-coerced records for
        ``telco_kyc_identity_document``.

    Example:
        >>> df = load_telco_kyc_identity_document_csv("telco_kyc_identity_document.csv", target_path="out/telco_kyc_identity_document.csv", mode="overwrite")

    Agent-use hint:
        Use this function after a user uploads CSV records for ``telco_kyc_identity_document``.
        Keep ``validate_required=True`` to catch incomplete input. Enable
        ``strict_columns=True`` only when the benchmark should fail files with
        extra source columns.
    """
    return load_table_csv(
        "telco_kyc_identity_document",
        csv_path=csv_path,
        existing_df=existing_df,
        target_path=target_path,
        mode=mode,
        output_format=output_format,
        validate_required=validate_required,
        strict_columns=strict_columns,
        add_missing_columns=add_missing_columns,
        read_csv_kwargs=read_csv_kwargs,
    )



def generate_telco_subscriber_sim_registry(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic pandas DataFrame for ``telco_subscriber_sim_registry``.

    Table definition:
        Subscriber and SIM registry. Represents active and historical SIM subscriptions, including Egyptian MSISDN, IMSI, ICCID, and operator-specific identifiers.

    Inputs:
        num_rows: Number of synthetic rows to generate. Use a small value such
            as 10 for unit tests and a larger value such as 10_000 for benchmark
            runs. Must be zero or positive.
        seed: Deterministic seed. The current generator is mostly deterministic
            by row index, but the seed is included for repeatable agent workflows
            and future stochastic variants.

    Returns:
        pandas.DataFrame with the exact column order defined for ``telco_subscriber_sim_registry``.

    Example:
        >>> df = generate_telco_subscriber_sim_registry(num_rows=100, seed=42)
        >>> df.columns.tolist() == get_table_columns("telco_subscriber_sim_registry")
        True

    Agent-use hint:
        Call this function when an agent needs realistic synthetic golden data
        for the ``telco_subscriber_sim_registry`` scenario before passing the DataFrame to a PII
        scanner, anonymizer, profiler, classifier, or data-quality test.
    """
    return generate_table("telco_subscriber_sim_registry", num_rows=num_rows, seed=seed)


def load_telco_subscriber_sim_registry_csv(
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records and add them to the pandas representation of ``telco_subscriber_sim_registry``.

    Table definition:
        Subscriber and SIM registry. Represents active and historical SIM subscriptions, including Egyptian MSISDN, IMSI, ICCID, and operator-specific identifiers.

    Inputs:
        csv_path: CSV file path containing rows to load for this table.
        existing_df: Optional existing pandas DataFrame. If provided, the loaded
            CSV rows are schema-normalized and appended to this DataFrame.
        target_path: Optional file path where the combined result should be
            written. The extension or ``output_format`` controls CSV, Parquet,
            JSON, or JSONL output.
        mode: Write behavior for ``target_path``. Use ``append`` to add records
            to an existing file, ``overwrite`` to replace it, or ``errorifexists``
            for defensive agent workflows.
        output_format: Optional explicit output format: ``csv``, ``parquet``,
            ``json``, or ``jsonl``.
        validate_required: When true, missing required columns raise an error.
        strict_columns: When true, unexpected source columns raise an error.
        add_missing_columns: When true, missing optional columns are added as
            ``pd.NA`` to preserve the schema contract.
        read_csv_kwargs: Optional dictionary passed to ``pandas.read_csv``.

    Returns:
        pandas.DataFrame containing schema-ordered, type-coerced records for
        ``telco_subscriber_sim_registry``.

    Example:
        >>> df = load_telco_subscriber_sim_registry_csv("telco_subscriber_sim_registry.csv", target_path="out/telco_subscriber_sim_registry.csv", mode="overwrite")

    Agent-use hint:
        Use this function after a user uploads CSV records for ``telco_subscriber_sim_registry``.
        Keep ``validate_required=True`` to catch incomplete input. Enable
        ``strict_columns=True`` only when the benchmark should fail files with
        extra source columns.
    """
    return load_table_csv(
        "telco_subscriber_sim_registry",
        csv_path=csv_path,
        existing_df=existing_df,
        target_path=target_path,
        mode=mode,
        output_format=output_format,
        validate_required=validate_required,
        strict_columns=strict_columns,
        add_missing_columns=add_missing_columns,
        read_csv_kwargs=read_csv_kwargs,
    )



def generate_telco_device_and_cpe_inventory(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic pandas DataFrame for ``telco_device_and_cpe_inventory``.

    Table definition:
        Customer device and CPE inventory. Tracks handset, router, ONT, and eSIM/device identifiers associated with subscribers and fixed broadband customers.

    Inputs:
        num_rows: Number of synthetic rows to generate. Use a small value such
            as 10 for unit tests and a larger value such as 10_000 for benchmark
            runs. Must be zero or positive.
        seed: Deterministic seed. The current generator is mostly deterministic
            by row index, but the seed is included for repeatable agent workflows
            and future stochastic variants.

    Returns:
        pandas.DataFrame with the exact column order defined for ``telco_device_and_cpe_inventory``.

    Example:
        >>> df = generate_telco_device_and_cpe_inventory(num_rows=100, seed=42)
        >>> df.columns.tolist() == get_table_columns("telco_device_and_cpe_inventory")
        True

    Agent-use hint:
        Call this function when an agent needs realistic synthetic golden data
        for the ``telco_device_and_cpe_inventory`` scenario before passing the DataFrame to a PII
        scanner, anonymizer, profiler, classifier, or data-quality test.
    """
    return generate_table("telco_device_and_cpe_inventory", num_rows=num_rows, seed=seed)


def load_telco_device_and_cpe_inventory_csv(
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records and add them to the pandas representation of ``telco_device_and_cpe_inventory``.

    Table definition:
        Customer device and CPE inventory. Tracks handset, router, ONT, and eSIM/device identifiers associated with subscribers and fixed broadband customers.

    Inputs:
        csv_path: CSV file path containing rows to load for this table.
        existing_df: Optional existing pandas DataFrame. If provided, the loaded
            CSV rows are schema-normalized and appended to this DataFrame.
        target_path: Optional file path where the combined result should be
            written. The extension or ``output_format`` controls CSV, Parquet,
            JSON, or JSONL output.
        mode: Write behavior for ``target_path``. Use ``append`` to add records
            to an existing file, ``overwrite`` to replace it, or ``errorifexists``
            for defensive agent workflows.
        output_format: Optional explicit output format: ``csv``, ``parquet``,
            ``json``, or ``jsonl``.
        validate_required: When true, missing required columns raise an error.
        strict_columns: When true, unexpected source columns raise an error.
        add_missing_columns: When true, missing optional columns are added as
            ``pd.NA`` to preserve the schema contract.
        read_csv_kwargs: Optional dictionary passed to ``pandas.read_csv``.

    Returns:
        pandas.DataFrame containing schema-ordered, type-coerced records for
        ``telco_device_and_cpe_inventory``.

    Example:
        >>> df = load_telco_device_and_cpe_inventory_csv("telco_device_and_cpe_inventory.csv", target_path="out/telco_device_and_cpe_inventory.csv", mode="overwrite")

    Agent-use hint:
        Use this function after a user uploads CSV records for ``telco_device_and_cpe_inventory``.
        Keep ``validate_required=True`` to catch incomplete input. Enable
        ``strict_columns=True`` only when the benchmark should fail files with
        extra source columns.
    """
    return load_table_csv(
        "telco_device_and_cpe_inventory",
        csv_path=csv_path,
        existing_df=existing_df,
        target_path=target_path,
        mode=mode,
        output_format=output_format,
        validate_required=validate_required,
        strict_columns=strict_columns,
        add_missing_columns=add_missing_columns,
        read_csv_kwargs=read_csv_kwargs,
    )



def generate_telco_cdr_event(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic pandas DataFrame for ``telco_cdr_event``.

    Table definition:
        Call detail record event. Synthetic call/SMS/data event table for testing PII detection in CDR-style transactional datasets.

    Inputs:
        num_rows: Number of synthetic rows to generate. Use a small value such
            as 10 for unit tests and a larger value such as 10_000 for benchmark
            runs. Must be zero or positive.
        seed: Deterministic seed. The current generator is mostly deterministic
            by row index, but the seed is included for repeatable agent workflows
            and future stochastic variants.

    Returns:
        pandas.DataFrame with the exact column order defined for ``telco_cdr_event``.

    Example:
        >>> df = generate_telco_cdr_event(num_rows=100, seed=42)
        >>> df.columns.tolist() == get_table_columns("telco_cdr_event")
        True

    Agent-use hint:
        Call this function when an agent needs realistic synthetic golden data
        for the ``telco_cdr_event`` scenario before passing the DataFrame to a PII
        scanner, anonymizer, profiler, classifier, or data-quality test.
    """
    return generate_table("telco_cdr_event", num_rows=num_rows, seed=seed)


def load_telco_cdr_event_csv(
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records and add them to the pandas representation of ``telco_cdr_event``.

    Table definition:
        Call detail record event. Synthetic call/SMS/data event table for testing PII detection in CDR-style transactional datasets.

    Inputs:
        csv_path: CSV file path containing rows to load for this table.
        existing_df: Optional existing pandas DataFrame. If provided, the loaded
            CSV rows are schema-normalized and appended to this DataFrame.
        target_path: Optional file path where the combined result should be
            written. The extension or ``output_format`` controls CSV, Parquet,
            JSON, or JSONL output.
        mode: Write behavior for ``target_path``. Use ``append`` to add records
            to an existing file, ``overwrite`` to replace it, or ``errorifexists``
            for defensive agent workflows.
        output_format: Optional explicit output format: ``csv``, ``parquet``,
            ``json``, or ``jsonl``.
        validate_required: When true, missing required columns raise an error.
        strict_columns: When true, unexpected source columns raise an error.
        add_missing_columns: When true, missing optional columns are added as
            ``pd.NA`` to preserve the schema contract.
        read_csv_kwargs: Optional dictionary passed to ``pandas.read_csv``.

    Returns:
        pandas.DataFrame containing schema-ordered, type-coerced records for
        ``telco_cdr_event``.

    Example:
        >>> df = load_telco_cdr_event_csv("telco_cdr_event.csv", target_path="out/telco_cdr_event.csv", mode="overwrite")

    Agent-use hint:
        Use this function after a user uploads CSV records for ``telco_cdr_event``.
        Keep ``validate_required=True`` to catch incomplete input. Enable
        ``strict_columns=True`` only when the benchmark should fail files with
        extra source columns.
    """
    return load_table_csv(
        "telco_cdr_event",
        csv_path=csv_path,
        existing_df=existing_df,
        target_path=target_path,
        mode=mode,
        output_format=output_format,
        validate_required=validate_required,
        strict_columns=strict_columns,
        add_missing_columns=add_missing_columns,
        read_csv_kwargs=read_csv_kwargs,
    )



def generate_telco_billing_invoice(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic pandas DataFrame for ``telco_billing_invoice``.

    Table definition:
        Telecom billing and invoice table. Billing-account and invoice data for postpaid, prepaid hybrid, and fixed-line telecom services.

    Inputs:
        num_rows: Number of synthetic rows to generate. Use a small value such
            as 10 for unit tests and a larger value such as 10_000 for benchmark
            runs. Must be zero or positive.
        seed: Deterministic seed. The current generator is mostly deterministic
            by row index, but the seed is included for repeatable agent workflows
            and future stochastic variants.

    Returns:
        pandas.DataFrame with the exact column order defined for ``telco_billing_invoice``.

    Example:
        >>> df = generate_telco_billing_invoice(num_rows=100, seed=42)
        >>> df.columns.tolist() == get_table_columns("telco_billing_invoice")
        True

    Agent-use hint:
        Call this function when an agent needs realistic synthetic golden data
        for the ``telco_billing_invoice`` scenario before passing the DataFrame to a PII
        scanner, anonymizer, profiler, classifier, or data-quality test.
    """
    return generate_table("telco_billing_invoice", num_rows=num_rows, seed=seed)


def load_telco_billing_invoice_csv(
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records and add them to the pandas representation of ``telco_billing_invoice``.

    Table definition:
        Telecom billing and invoice table. Billing-account and invoice data for postpaid, prepaid hybrid, and fixed-line telecom services.

    Inputs:
        csv_path: CSV file path containing rows to load for this table.
        existing_df: Optional existing pandas DataFrame. If provided, the loaded
            CSV rows are schema-normalized and appended to this DataFrame.
        target_path: Optional file path where the combined result should be
            written. The extension or ``output_format`` controls CSV, Parquet,
            JSON, or JSONL output.
        mode: Write behavior for ``target_path``. Use ``append`` to add records
            to an existing file, ``overwrite`` to replace it, or ``errorifexists``
            for defensive agent workflows.
        output_format: Optional explicit output format: ``csv``, ``parquet``,
            ``json``, or ``jsonl``.
        validate_required: When true, missing required columns raise an error.
        strict_columns: When true, unexpected source columns raise an error.
        add_missing_columns: When true, missing optional columns are added as
            ``pd.NA`` to preserve the schema contract.
        read_csv_kwargs: Optional dictionary passed to ``pandas.read_csv``.

    Returns:
        pandas.DataFrame containing schema-ordered, type-coerced records for
        ``telco_billing_invoice``.

    Example:
        >>> df = load_telco_billing_invoice_csv("telco_billing_invoice.csv", target_path="out/telco_billing_invoice.csv", mode="overwrite")

    Agent-use hint:
        Use this function after a user uploads CSV records for ``telco_billing_invoice``.
        Keep ``validate_required=True`` to catch incomplete input. Enable
        ``strict_columns=True`` only when the benchmark should fail files with
        extra source columns.
    """
    return load_table_csv(
        "telco_billing_invoice",
        csv_path=csv_path,
        existing_df=existing_df,
        target_path=target_path,
        mode=mode,
        output_format=output_format,
        validate_required=validate_required,
        strict_columns=strict_columns,
        add_missing_columns=add_missing_columns,
        read_csv_kwargs=read_csv_kwargs,
    )



def generate_eshop_customer_account(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic pandas DataFrame for ``eshop_customer_account``.

    Table definition:
        E-commerce customer account. Customer account table for Egyptian e-commerce, marketplace, and digital retail use cases.

    Inputs:
        num_rows: Number of synthetic rows to generate. Use a small value such
            as 10 for unit tests and a larger value such as 10_000 for benchmark
            runs. Must be zero or positive.
        seed: Deterministic seed. The current generator is mostly deterministic
            by row index, but the seed is included for repeatable agent workflows
            and future stochastic variants.

    Returns:
        pandas.DataFrame with the exact column order defined for ``eshop_customer_account``.

    Example:
        >>> df = generate_eshop_customer_account(num_rows=100, seed=42)
        >>> df.columns.tolist() == get_table_columns("eshop_customer_account")
        True

    Agent-use hint:
        Call this function when an agent needs realistic synthetic golden data
        for the ``eshop_customer_account`` scenario before passing the DataFrame to a PII
        scanner, anonymizer, profiler, classifier, or data-quality test.
    """
    return generate_table("eshop_customer_account", num_rows=num_rows, seed=seed)


def load_eshop_customer_account_csv(
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records and add them to the pandas representation of ``eshop_customer_account``.

    Table definition:
        E-commerce customer account. Customer account table for Egyptian e-commerce, marketplace, and digital retail use cases.

    Inputs:
        csv_path: CSV file path containing rows to load for this table.
        existing_df: Optional existing pandas DataFrame. If provided, the loaded
            CSV rows are schema-normalized and appended to this DataFrame.
        target_path: Optional file path where the combined result should be
            written. The extension or ``output_format`` controls CSV, Parquet,
            JSON, or JSONL output.
        mode: Write behavior for ``target_path``. Use ``append`` to add records
            to an existing file, ``overwrite`` to replace it, or ``errorifexists``
            for defensive agent workflows.
        output_format: Optional explicit output format: ``csv``, ``parquet``,
            ``json``, or ``jsonl``.
        validate_required: When true, missing required columns raise an error.
        strict_columns: When true, unexpected source columns raise an error.
        add_missing_columns: When true, missing optional columns are added as
            ``pd.NA`` to preserve the schema contract.
        read_csv_kwargs: Optional dictionary passed to ``pandas.read_csv``.

    Returns:
        pandas.DataFrame containing schema-ordered, type-coerced records for
        ``eshop_customer_account``.

    Example:
        >>> df = load_eshop_customer_account_csv("eshop_customer_account.csv", target_path="out/eshop_customer_account.csv", mode="overwrite")

    Agent-use hint:
        Use this function after a user uploads CSV records for ``eshop_customer_account``.
        Keep ``validate_required=True`` to catch incomplete input. Enable
        ``strict_columns=True`` only when the benchmark should fail files with
        extra source columns.
    """
    return load_table_csv(
        "eshop_customer_account",
        csv_path=csv_path,
        existing_df=existing_df,
        target_path=target_path,
        mode=mode,
        output_format=output_format,
        validate_required=validate_required,
        strict_columns=strict_columns,
        add_missing_columns=add_missing_columns,
        read_csv_kwargs=read_csv_kwargs,
    )



def generate_eshop_shipping_address(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic pandas DataFrame for ``eshop_shipping_address``.

    Table definition:
        E-commerce shipping address book. Address-book table containing Egyptian delivery-address components needed for e-commerce fulfillment.

    Inputs:
        num_rows: Number of synthetic rows to generate. Use a small value such
            as 10 for unit tests and a larger value such as 10_000 for benchmark
            runs. Must be zero or positive.
        seed: Deterministic seed. The current generator is mostly deterministic
            by row index, but the seed is included for repeatable agent workflows
            and future stochastic variants.

    Returns:
        pandas.DataFrame with the exact column order defined for ``eshop_shipping_address``.

    Example:
        >>> df = generate_eshop_shipping_address(num_rows=100, seed=42)
        >>> df.columns.tolist() == get_table_columns("eshop_shipping_address")
        True

    Agent-use hint:
        Call this function when an agent needs realistic synthetic golden data
        for the ``eshop_shipping_address`` scenario before passing the DataFrame to a PII
        scanner, anonymizer, profiler, classifier, or data-quality test.
    """
    return generate_table("eshop_shipping_address", num_rows=num_rows, seed=seed)


def load_eshop_shipping_address_csv(
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records and add them to the pandas representation of ``eshop_shipping_address``.

    Table definition:
        E-commerce shipping address book. Address-book table containing Egyptian delivery-address components needed for e-commerce fulfillment.

    Inputs:
        csv_path: CSV file path containing rows to load for this table.
        existing_df: Optional existing pandas DataFrame. If provided, the loaded
            CSV rows are schema-normalized and appended to this DataFrame.
        target_path: Optional file path where the combined result should be
            written. The extension or ``output_format`` controls CSV, Parquet,
            JSON, or JSONL output.
        mode: Write behavior for ``target_path``. Use ``append`` to add records
            to an existing file, ``overwrite`` to replace it, or ``errorifexists``
            for defensive agent workflows.
        output_format: Optional explicit output format: ``csv``, ``parquet``,
            ``json``, or ``jsonl``.
        validate_required: When true, missing required columns raise an error.
        strict_columns: When true, unexpected source columns raise an error.
        add_missing_columns: When true, missing optional columns are added as
            ``pd.NA`` to preserve the schema contract.
        read_csv_kwargs: Optional dictionary passed to ``pandas.read_csv``.

    Returns:
        pandas.DataFrame containing schema-ordered, type-coerced records for
        ``eshop_shipping_address``.

    Example:
        >>> df = load_eshop_shipping_address_csv("eshop_shipping_address.csv", target_path="out/eshop_shipping_address.csv", mode="overwrite")

    Agent-use hint:
        Use this function after a user uploads CSV records for ``eshop_shipping_address``.
        Keep ``validate_required=True`` to catch incomplete input. Enable
        ``strict_columns=True`` only when the benchmark should fail files with
        extra source columns.
    """
    return load_table_csv(
        "eshop_shipping_address",
        csv_path=csv_path,
        existing_df=existing_df,
        target_path=target_path,
        mode=mode,
        output_format=output_format,
        validate_required=validate_required,
        strict_columns=strict_columns,
        add_missing_columns=add_missing_columns,
        read_csv_kwargs=read_csv_kwargs,
    )



def generate_eshop_order_header(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic pandas DataFrame for ``eshop_order_header``.

    Table definition:
        E-commerce order header. Order-level table connecting customers, delivery address, payment channel, and fulfillment status.

    Inputs:
        num_rows: Number of synthetic rows to generate. Use a small value such
            as 10 for unit tests and a larger value such as 10_000 for benchmark
            runs. Must be zero or positive.
        seed: Deterministic seed. The current generator is mostly deterministic
            by row index, but the seed is included for repeatable agent workflows
            and future stochastic variants.

    Returns:
        pandas.DataFrame with the exact column order defined for ``eshop_order_header``.

    Example:
        >>> df = generate_eshop_order_header(num_rows=100, seed=42)
        >>> df.columns.tolist() == get_table_columns("eshop_order_header")
        True

    Agent-use hint:
        Call this function when an agent needs realistic synthetic golden data
        for the ``eshop_order_header`` scenario before passing the DataFrame to a PII
        scanner, anonymizer, profiler, classifier, or data-quality test.
    """
    return generate_table("eshop_order_header", num_rows=num_rows, seed=seed)


def load_eshop_order_header_csv(
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records and add them to the pandas representation of ``eshop_order_header``.

    Table definition:
        E-commerce order header. Order-level table connecting customers, delivery address, payment channel, and fulfillment status.

    Inputs:
        csv_path: CSV file path containing rows to load for this table.
        existing_df: Optional existing pandas DataFrame. If provided, the loaded
            CSV rows are schema-normalized and appended to this DataFrame.
        target_path: Optional file path where the combined result should be
            written. The extension or ``output_format`` controls CSV, Parquet,
            JSON, or JSONL output.
        mode: Write behavior for ``target_path``. Use ``append`` to add records
            to an existing file, ``overwrite`` to replace it, or ``errorifexists``
            for defensive agent workflows.
        output_format: Optional explicit output format: ``csv``, ``parquet``,
            ``json``, or ``jsonl``.
        validate_required: When true, missing required columns raise an error.
        strict_columns: When true, unexpected source columns raise an error.
        add_missing_columns: When true, missing optional columns are added as
            ``pd.NA`` to preserve the schema contract.
        read_csv_kwargs: Optional dictionary passed to ``pandas.read_csv``.

    Returns:
        pandas.DataFrame containing schema-ordered, type-coerced records for
        ``eshop_order_header``.

    Example:
        >>> df = load_eshop_order_header_csv("eshop_order_header.csv", target_path="out/eshop_order_header.csv", mode="overwrite")

    Agent-use hint:
        Use this function after a user uploads CSV records for ``eshop_order_header``.
        Keep ``validate_required=True`` to catch incomplete input. Enable
        ``strict_columns=True`` only when the benchmark should fail files with
        extra source columns.
    """
    return load_table_csv(
        "eshop_order_header",
        csv_path=csv_path,
        existing_df=existing_df,
        target_path=target_path,
        mode=mode,
        output_format=output_format,
        validate_required=validate_required,
        strict_columns=strict_columns,
        add_missing_columns=add_missing_columns,
        read_csv_kwargs=read_csv_kwargs,
    )



def generate_fintech_payment_transaction(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic pandas DataFrame for ``fintech_payment_transaction``.

    Table definition:
        Egypt fintech payment transaction. Unified payment-transaction table covering card, Meeza, Fawry, mobile-wallet, InstaPay, and bank-transfer flows used by telecom and e-commerce.

    Inputs:
        num_rows: Number of synthetic rows to generate. Use a small value such
            as 10 for unit tests and a larger value such as 10_000 for benchmark
            runs. Must be zero or positive.
        seed: Deterministic seed. The current generator is mostly deterministic
            by row index, but the seed is included for repeatable agent workflows
            and future stochastic variants.

    Returns:
        pandas.DataFrame with the exact column order defined for ``fintech_payment_transaction``.

    Example:
        >>> df = generate_fintech_payment_transaction(num_rows=100, seed=42)
        >>> df.columns.tolist() == get_table_columns("fintech_payment_transaction")
        True

    Agent-use hint:
        Call this function when an agent needs realistic synthetic golden data
        for the ``fintech_payment_transaction`` scenario before passing the DataFrame to a PII
        scanner, anonymizer, profiler, classifier, or data-quality test.
    """
    return generate_table("fintech_payment_transaction", num_rows=num_rows, seed=seed)


def load_fintech_payment_transaction_csv(
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records and add them to the pandas representation of ``fintech_payment_transaction``.

    Table definition:
        Egypt fintech payment transaction. Unified payment-transaction table covering card, Meeza, Fawry, mobile-wallet, InstaPay, and bank-transfer flows used by telecom and e-commerce.

    Inputs:
        csv_path: CSV file path containing rows to load for this table.
        existing_df: Optional existing pandas DataFrame. If provided, the loaded
            CSV rows are schema-normalized and appended to this DataFrame.
        target_path: Optional file path where the combined result should be
            written. The extension or ``output_format`` controls CSV, Parquet,
            JSON, or JSONL output.
        mode: Write behavior for ``target_path``. Use ``append`` to add records
            to an existing file, ``overwrite`` to replace it, or ``errorifexists``
            for defensive agent workflows.
        output_format: Optional explicit output format: ``csv``, ``parquet``,
            ``json``, or ``jsonl``.
        validate_required: When true, missing required columns raise an error.
        strict_columns: When true, unexpected source columns raise an error.
        add_missing_columns: When true, missing optional columns are added as
            ``pd.NA`` to preserve the schema contract.
        read_csv_kwargs: Optional dictionary passed to ``pandas.read_csv``.

    Returns:
        pandas.DataFrame containing schema-ordered, type-coerced records for
        ``fintech_payment_transaction``.

    Example:
        >>> df = load_fintech_payment_transaction_csv("fintech_payment_transaction.csv", target_path="out/fintech_payment_transaction.csv", mode="overwrite")

    Agent-use hint:
        Use this function after a user uploads CSV records for ``fintech_payment_transaction``.
        Keep ``validate_required=True`` to catch incomplete input. Enable
        ``strict_columns=True`` only when the benchmark should fail files with
        extra source columns.
    """
    return load_table_csv(
        "fintech_payment_transaction",
        csv_path=csv_path,
        existing_df=existing_df,
        target_path=target_path,
        mode=mode,
        output_format=output_format,
        validate_required=validate_required,
        strict_columns=strict_columns,
        add_missing_columns=add_missing_columns,
        read_csv_kwargs=read_csv_kwargs,
    )



def generate_fintech_mobile_wallet_account(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic pandas DataFrame for ``fintech_mobile_wallet_account``.

    Table definition:
        Egypt mobile wallet account. Mobile wallet account table for Vodafone Cash, Orange Cash, Etisalat Cash, WE Pay, and similar Egyptian wallet products.

    Inputs:
        num_rows: Number of synthetic rows to generate. Use a small value such
            as 10 for unit tests and a larger value such as 10_000 for benchmark
            runs. Must be zero or positive.
        seed: Deterministic seed. The current generator is mostly deterministic
            by row index, but the seed is included for repeatable agent workflows
            and future stochastic variants.

    Returns:
        pandas.DataFrame with the exact column order defined for ``fintech_mobile_wallet_account``.

    Example:
        >>> df = generate_fintech_mobile_wallet_account(num_rows=100, seed=42)
        >>> df.columns.tolist() == get_table_columns("fintech_mobile_wallet_account")
        True

    Agent-use hint:
        Call this function when an agent needs realistic synthetic golden data
        for the ``fintech_mobile_wallet_account`` scenario before passing the DataFrame to a PII
        scanner, anonymizer, profiler, classifier, or data-quality test.
    """
    return generate_table("fintech_mobile_wallet_account", num_rows=num_rows, seed=seed)


def load_fintech_mobile_wallet_account_csv(
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records and add them to the pandas representation of ``fintech_mobile_wallet_account``.

    Table definition:
        Egypt mobile wallet account. Mobile wallet account table for Vodafone Cash, Orange Cash, Etisalat Cash, WE Pay, and similar Egyptian wallet products.

    Inputs:
        csv_path: CSV file path containing rows to load for this table.
        existing_df: Optional existing pandas DataFrame. If provided, the loaded
            CSV rows are schema-normalized and appended to this DataFrame.
        target_path: Optional file path where the combined result should be
            written. The extension or ``output_format`` controls CSV, Parquet,
            JSON, or JSONL output.
        mode: Write behavior for ``target_path``. Use ``append`` to add records
            to an existing file, ``overwrite`` to replace it, or ``errorifexists``
            for defensive agent workflows.
        output_format: Optional explicit output format: ``csv``, ``parquet``,
            ``json``, or ``jsonl``.
        validate_required: When true, missing required columns raise an error.
        strict_columns: When true, unexpected source columns raise an error.
        add_missing_columns: When true, missing optional columns are added as
            ``pd.NA`` to preserve the schema contract.
        read_csv_kwargs: Optional dictionary passed to ``pandas.read_csv``.

    Returns:
        pandas.DataFrame containing schema-ordered, type-coerced records for
        ``fintech_mobile_wallet_account``.

    Example:
        >>> df = load_fintech_mobile_wallet_account_csv("fintech_mobile_wallet_account.csv", target_path="out/fintech_mobile_wallet_account.csv", mode="overwrite")

    Agent-use hint:
        Use this function after a user uploads CSV records for ``fintech_mobile_wallet_account``.
        Keep ``validate_required=True`` to catch incomplete input. Enable
        ``strict_columns=True`` only when the benchmark should fail files with
        extra source columns.
    """
    return load_table_csv(
        "fintech_mobile_wallet_account",
        csv_path=csv_path,
        existing_df=existing_df,
        target_path=target_path,
        mode=mode,
        output_format=output_format,
        validate_required=validate_required,
        strict_columns=strict_columns,
        add_missing_columns=add_missing_columns,
        read_csv_kwargs=read_csv_kwargs,
    )



def generate_merchant_seller_registry(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic pandas DataFrame for ``merchant_seller_registry``.

    Table definition:
        Marketplace merchant and seller registry. Business seller registry for Egyptian e-commerce marketplaces, covering tax, commercial, settlement, and contact identifiers.

    Inputs:
        num_rows: Number of synthetic rows to generate. Use a small value such
            as 10 for unit tests and a larger value such as 10_000 for benchmark
            runs. Must be zero or positive.
        seed: Deterministic seed. The current generator is mostly deterministic
            by row index, but the seed is included for repeatable agent workflows
            and future stochastic variants.

    Returns:
        pandas.DataFrame with the exact column order defined for ``merchant_seller_registry``.

    Example:
        >>> df = generate_merchant_seller_registry(num_rows=100, seed=42)
        >>> df.columns.tolist() == get_table_columns("merchant_seller_registry")
        True

    Agent-use hint:
        Call this function when an agent needs realistic synthetic golden data
        for the ``merchant_seller_registry`` scenario before passing the DataFrame to a PII
        scanner, anonymizer, profiler, classifier, or data-quality test.
    """
    return generate_table("merchant_seller_registry", num_rows=num_rows, seed=seed)


def load_merchant_seller_registry_csv(
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records and add them to the pandas representation of ``merchant_seller_registry``.

    Table definition:
        Marketplace merchant and seller registry. Business seller registry for Egyptian e-commerce marketplaces, covering tax, commercial, settlement, and contact identifiers.

    Inputs:
        csv_path: CSV file path containing rows to load for this table.
        existing_df: Optional existing pandas DataFrame. If provided, the loaded
            CSV rows are schema-normalized and appended to this DataFrame.
        target_path: Optional file path where the combined result should be
            written. The extension or ``output_format`` controls CSV, Parquet,
            JSON, or JSONL output.
        mode: Write behavior for ``target_path``. Use ``append`` to add records
            to an existing file, ``overwrite`` to replace it, or ``errorifexists``
            for defensive agent workflows.
        output_format: Optional explicit output format: ``csv``, ``parquet``,
            ``json``, or ``jsonl``.
        validate_required: When true, missing required columns raise an error.
        strict_columns: When true, unexpected source columns raise an error.
        add_missing_columns: When true, missing optional columns are added as
            ``pd.NA`` to preserve the schema contract.
        read_csv_kwargs: Optional dictionary passed to ``pandas.read_csv``.

    Returns:
        pandas.DataFrame containing schema-ordered, type-coerced records for
        ``merchant_seller_registry``.

    Example:
        >>> df = load_merchant_seller_registry_csv("merchant_seller_registry.csv", target_path="out/merchant_seller_registry.csv", mode="overwrite")

    Agent-use hint:
        Use this function after a user uploads CSV records for ``merchant_seller_registry``.
        Keep ``validate_required=True`` to catch incomplete input. Enable
        ``strict_columns=True`` only when the benchmark should fail files with
        extra source columns.
    """
    return load_table_csv(
        "merchant_seller_registry",
        csv_path=csv_path,
        existing_df=existing_df,
        target_path=target_path,
        mode=mode,
        output_format=output_format,
        validate_required=validate_required,
        strict_columns=strict_columns,
        add_missing_columns=add_missing_columns,
        read_csv_kwargs=read_csv_kwargs,
    )



def generate_support_ticket_free_text(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic pandas DataFrame for ``support_ticket_free_text``.

    Table definition:
        Support ticket and free-text interaction log. Free-text ticket data for evaluating entity extraction from notes, SMS bodies, chat transcripts, emails, and logs.

    Inputs:
        num_rows: Number of synthetic rows to generate. Use a small value such
            as 10 for unit tests and a larger value such as 10_000 for benchmark
            runs. Must be zero or positive.
        seed: Deterministic seed. The current generator is mostly deterministic
            by row index, but the seed is included for repeatable agent workflows
            and future stochastic variants.

    Returns:
        pandas.DataFrame with the exact column order defined for ``support_ticket_free_text``.

    Example:
        >>> df = generate_support_ticket_free_text(num_rows=100, seed=42)
        >>> df.columns.tolist() == get_table_columns("support_ticket_free_text")
        True

    Agent-use hint:
        Call this function when an agent needs realistic synthetic golden data
        for the ``support_ticket_free_text`` scenario before passing the DataFrame to a PII
        scanner, anonymizer, profiler, classifier, or data-quality test.
    """
    return generate_table("support_ticket_free_text", num_rows=num_rows, seed=seed)


def load_support_ticket_free_text_csv(
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records and add them to the pandas representation of ``support_ticket_free_text``.

    Table definition:
        Support ticket and free-text interaction log. Free-text ticket data for evaluating entity extraction from notes, SMS bodies, chat transcripts, emails, and logs.

    Inputs:
        csv_path: CSV file path containing rows to load for this table.
        existing_df: Optional existing pandas DataFrame. If provided, the loaded
            CSV rows are schema-normalized and appended to this DataFrame.
        target_path: Optional file path where the combined result should be
            written. The extension or ``output_format`` controls CSV, Parquet,
            JSON, or JSONL output.
        mode: Write behavior for ``target_path``. Use ``append`` to add records
            to an existing file, ``overwrite`` to replace it, or ``errorifexists``
            for defensive agent workflows.
        output_format: Optional explicit output format: ``csv``, ``parquet``,
            ``json``, or ``jsonl``.
        validate_required: When true, missing required columns raise an error.
        strict_columns: When true, unexpected source columns raise an error.
        add_missing_columns: When true, missing optional columns are added as
            ``pd.NA`` to preserve the schema contract.
        read_csv_kwargs: Optional dictionary passed to ``pandas.read_csv``.

    Returns:
        pandas.DataFrame containing schema-ordered, type-coerced records for
        ``support_ticket_free_text``.

    Example:
        >>> df = load_support_ticket_free_text_csv("support_ticket_free_text.csv", target_path="out/support_ticket_free_text.csv", mode="overwrite")

    Agent-use hint:
        Use this function after a user uploads CSV records for ``support_ticket_free_text``.
        Keep ``validate_required=True`` to catch incomplete input. Enable
        ``strict_columns=True`` only when the benchmark should fail files with
        extra source columns.
    """
    return load_table_csv(
        "support_ticket_free_text",
        csv_path=csv_path,
        existing_df=existing_df,
        target_path=target_path,
        mode=mode,
        output_format=output_format,
        validate_required=validate_required,
        strict_columns=strict_columns,
        add_missing_columns=add_missing_columns,
        read_csv_kwargs=read_csv_kwargs,
    )



def generate_sensitive_customer_risk_profile(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic pandas DataFrame for ``sensitive_customer_risk_profile``.

    Table definition:
        Sensitive customer risk and eligibility profile. Synthetic table for high-risk sensitive personal data classes under privacy and sector controls. Use only synthetic values.

    Inputs:
        num_rows: Number of synthetic rows to generate. Use a small value such
            as 10 for unit tests and a larger value such as 10_000 for benchmark
            runs. Must be zero or positive.
        seed: Deterministic seed. The current generator is mostly deterministic
            by row index, but the seed is included for repeatable agent workflows
            and future stochastic variants.

    Returns:
        pandas.DataFrame with the exact column order defined for ``sensitive_customer_risk_profile``.

    Example:
        >>> df = generate_sensitive_customer_risk_profile(num_rows=100, seed=42)
        >>> df.columns.tolist() == get_table_columns("sensitive_customer_risk_profile")
        True

    Agent-use hint:
        Call this function when an agent needs realistic synthetic golden data
        for the ``sensitive_customer_risk_profile`` scenario before passing the DataFrame to a PII
        scanner, anonymizer, profiler, classifier, or data-quality test.
    """
    return generate_table("sensitive_customer_risk_profile", num_rows=num_rows, seed=seed)


def load_sensitive_customer_risk_profile_csv(
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records and add them to the pandas representation of ``sensitive_customer_risk_profile``.

    Table definition:
        Sensitive customer risk and eligibility profile. Synthetic table for high-risk sensitive personal data classes under privacy and sector controls. Use only synthetic values.

    Inputs:
        csv_path: CSV file path containing rows to load for this table.
        existing_df: Optional existing pandas DataFrame. If provided, the loaded
            CSV rows are schema-normalized and appended to this DataFrame.
        target_path: Optional file path where the combined result should be
            written. The extension or ``output_format`` controls CSV, Parquet,
            JSON, or JSONL output.
        mode: Write behavior for ``target_path``. Use ``append`` to add records
            to an existing file, ``overwrite`` to replace it, or ``errorifexists``
            for defensive agent workflows.
        output_format: Optional explicit output format: ``csv``, ``parquet``,
            ``json``, or ``jsonl``.
        validate_required: When true, missing required columns raise an error.
        strict_columns: When true, unexpected source columns raise an error.
        add_missing_columns: When true, missing optional columns are added as
            ``pd.NA`` to preserve the schema contract.
        read_csv_kwargs: Optional dictionary passed to ``pandas.read_csv``.

    Returns:
        pandas.DataFrame containing schema-ordered, type-coerced records for
        ``sensitive_customer_risk_profile``.

    Example:
        >>> df = load_sensitive_customer_risk_profile_csv("sensitive_customer_risk_profile.csv", target_path="out/sensitive_customer_risk_profile.csv", mode="overwrite")

    Agent-use hint:
        Use this function after a user uploads CSV records for ``sensitive_customer_risk_profile``.
        Keep ``validate_required=True`` to catch incomplete input. Enable
        ``strict_columns=True`` only when the benchmark should fail files with
        extra source columns.
    """
    return load_table_csv(
        "sensitive_customer_risk_profile",
        csv_path=csv_path,
        existing_df=existing_df,
        target_path=target_path,
        mode=mode,
        output_format=output_format,
        validate_required=validate_required,
        strict_columns=strict_columns,
        add_missing_columns=add_missing_columns,
        read_csv_kwargs=read_csv_kwargs,
    )


GENERATOR_FUNCTIONS: Dict[str, Any] = {
    "telco_customer_profile": generate_telco_customer_profile,
    "telco_kyc_identity_document": generate_telco_kyc_identity_document,
    "telco_subscriber_sim_registry": generate_telco_subscriber_sim_registry,
    "telco_device_and_cpe_inventory": generate_telco_device_and_cpe_inventory,
    "telco_cdr_event": generate_telco_cdr_event,
    "telco_billing_invoice": generate_telco_billing_invoice,
    "eshop_customer_account": generate_eshop_customer_account,
    "eshop_shipping_address": generate_eshop_shipping_address,
    "eshop_order_header": generate_eshop_order_header,
    "fintech_payment_transaction": generate_fintech_payment_transaction,
    "fintech_mobile_wallet_account": generate_fintech_mobile_wallet_account,
    "merchant_seller_registry": generate_merchant_seller_registry,
    "support_ticket_free_text": generate_support_ticket_free_text,
    "sensitive_customer_risk_profile": generate_sensitive_customer_risk_profile,
}

CSV_LOADER_FUNCTIONS: Dict[str, Any] = {
    "telco_customer_profile": load_telco_customer_profile_csv,
    "telco_kyc_identity_document": load_telco_kyc_identity_document_csv,
    "telco_subscriber_sim_registry": load_telco_subscriber_sim_registry_csv,
    "telco_device_and_cpe_inventory": load_telco_device_and_cpe_inventory_csv,
    "telco_cdr_event": load_telco_cdr_event_csv,
    "telco_billing_invoice": load_telco_billing_invoice_csv,
    "eshop_customer_account": load_eshop_customer_account_csv,
    "eshop_shipping_address": load_eshop_shipping_address_csv,
    "eshop_order_header": load_eshop_order_header_csv,
    "fintech_payment_transaction": load_fintech_payment_transaction_csv,
    "fintech_mobile_wallet_account": load_fintech_mobile_wallet_account_csv,
    "merchant_seller_registry": load_merchant_seller_registry_csv,
    "support_ticket_free_text": load_support_ticket_free_text_csv,
    "sensitive_customer_risk_profile": load_sensitive_customer_risk_profile_csv,
}
