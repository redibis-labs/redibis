"""
Egypt Telecom, E-commerce, and Fintech PII Synthetic Data Utilities for PySpark.

This module contains two public functions for each of the 14 ODCS-style schema
objects in the Egyptian PII golden-data catalogue:

1. ``generate_<table_name>(spark, num_rows, seed=42)`` creates a synthetic
   PySpark DataFrame with realistic but fake values aligned to the table schema.
2. ``load_<table_name>_csv(spark, csv_path, target_table=None, target_path=None, ...)``
   reads a CSV file, coerces/reorders it to the expected schema, and optionally
   appends it to a Spark table or storage path.

The generated values are intentionally synthetic. They are suitable for PII
framework evaluation, data-quality testing, and agent-driven benchmark creation,
but they must not be treated as production records.

Agent-tool usage hint:
    Import this module inside a PySpark-capable runtime, create or reuse a
    SparkSession, call a generator function with the desired row count, then
    pass the resulting DataFrame to the target scanner, anonymizer, classifier,
    or data-contract validation tool. For CSV ingestion tasks, call the matching
    ``load_*_csv`` function and pass the path provided by the user or agent.
"""

from __future__ import annotations

import hashlib
import random
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

TABLE_SCHEMAS: Dict[str, Dict[str, Any]] = {'telco_customer_profile': {'businessName': 'Telecom customer master profile', 'description': 'Core customer profile used by CRM, billing, support, marketing consent, and KYC workflows for Egyptian telecom operators.', 'domain': 'telecom_crm', 'granularity': 'One row per natural person or business customer account holder.', 'columns': [{'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal customer key used across CRM and billing.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'full_name_en', 'businessName': 'Full legal name in English', 'logicalType': 'string', 'physicalType': 'VARCHAR(160)', 'description': 'Customer name written in Latin characters.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'full_name_ar', 'businessName': 'Full legal name in Arabic', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Customer name written in Arabic characters.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'primary_mobile', 'businessName': 'Primary Egyptian mobile number', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Primary contact number, normally normalized to E.164.', 'required': True, 'unique': True, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EGYPTIAN_MOBILE', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'alternate_phone', 'businessName': 'Alternate contact phone', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Secondary mobile or landline number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PHONE_NUMBER', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'email_address', 'businessName': 'Email address', 'logicalType': 'string', 'physicalType': 'VARCHAR(254)', 'description': 'Customer email used for notifications and account recovery.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EMAIL_ADDRESS', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'date_of_birth', 'businessName': 'Date of birth', 'logicalType': 'date', 'physicalType': 'DATE', 'description': 'Customer birth date.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'DATE_OF_BIRTH', 'is_pii': True, 'pii_category': 'quasi_identifier', 'sensitivity_level': 'high'}, {'name': 'gender', 'businessName': 'Gender', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Customer gender as collected in CRM/KYC.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'GENDER_INDICATOR', 'is_pii': True, 'pii_category': 'demographic', 'sensitivity_level': 'restricted'}, {'name': 'nationality', 'businessName': 'Nationality', 'logicalType': 'string', 'physicalType': 'CHAR(3)', 'description': 'ISO alpha-3 nationality code.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'NRP', 'is_pii': True, 'pii_category': 'sensitive_demographic', 'sensitivity_level': 'restricted'}, {'name': 'marketing_consent_flag', 'businessName': 'Marketing consent flag', 'logicalType': 'boolean', 'physicalType': 'BOOLEAN', 'description': 'Indicates whether customer opted in to promotional communication.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CONSENT_STATUS', 'is_pii': False, 'pii_category': 'privacy_preference', 'sensitivity_level': 'internal'}, {'name': 'created_at', 'businessName': 'Creation timestamp', 'logicalType': 'timestamp', 'physicalType': 'TIMESTAMP', 'description': 'Record creation timestamp.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'TIMESTAMP', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'telco_kyc_identity_document': {'businessName': 'Telecom KYC identity document registry', 'description': 'Stores identity-document attributes captured during SIM registration, account opening, or customer due diligence.', 'domain': 'telecom_kyc', 'granularity': 'One row per identity document submitted by a customer.', 'columns': [{'name': 'kyc_document_id', 'businessName': 'KYC document identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal KYC document key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Foreign key to telecom customer profile.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'eg_national_id', 'businessName': 'Egyptian national ID', 'logicalType': 'string', 'physicalType': 'CHAR(14)', 'description': '14-digit Egyptian National ID number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_NATIONAL_ID', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'passport_number', 'businessName': 'Passport number', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Passport number for Egyptian or non-Egyptian customers.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_PASSPORT', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'residency_permit_number', 'businessName': 'Residency permit number', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Permit number for non-Egyptian residents.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_RESIDENCY_PERMIT', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'driver_license_number', 'businessName': 'Driver license number', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Egyptian driver license or equivalent supporting ID.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_DRIVER_LICENSE', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'military_id', 'businessName': 'Military ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Military identity number where applicable.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_MILITARY_ID', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'syndicate_id', 'businessName': 'Professional syndicate ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(40)', 'description': 'Professional association card number such as doctors or engineers syndicate.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EG_SYNDICATE_ID', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'document_image_ref', 'businessName': 'Document image reference', 'logicalType': 'string', 'physicalType': 'VARCHAR(256)', 'description': 'Object-store reference to scanned identity image; do not store raw image in test data.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'BIOMETRIC_DATA', 'is_pii': True, 'pii_category': 'sensitive_document', 'sensitivity_level': 'critical'}, {'name': 'kyc_verified_at', 'businessName': 'KYC verification timestamp', 'logicalType': 'timestamp', 'physicalType': 'TIMESTAMP', 'description': 'Timestamp when KYC verification completed.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'TIMESTAMP', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'telco_subscriber_sim_registry': {'businessName': 'Subscriber and SIM registry', 'description': 'Represents active and historical SIM subscriptions, including Egyptian MSISDN, IMSI, ICCID, and operator-specific identifiers.', 'domain': 'telecom_bss', 'granularity': 'One row per SIM subscription lifecycle instance.', 'columns': [{'name': 'subscription_id', 'businessName': 'Subscription identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal subscription key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'SUBSCRIBER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Customer owning or using the subscription.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'msisdn', 'businessName': 'Mobile station ISDN number', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Customer-facing mobile number in Egyptian or E.164 format.', 'required': True, 'unique': True, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'MSISDN', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'operator_original_prefix', 'businessName': 'Original allocation operator', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Operator inferred from original Egyptian prefix; MNP may change actual operator.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CATEGORY', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'imsi', 'businessName': 'International Mobile Subscriber Identity', 'logicalType': 'string', 'physicalType': 'CHAR(15)', 'description': 'SIM subscriber identity used in mobile networks.', 'required': True, 'unique': True, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IMSI', 'is_pii': True, 'pii_category': 'telecom_identifier', 'sensitivity_level': 'critical'}, {'name': 'iccid', 'businessName': 'Integrated Circuit Card Identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(22)', 'description': 'SIM card serial number.', 'required': True, 'unique': True, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'ICCID', 'is_pii': True, 'pii_category': 'telecom_identifier', 'sensitivity_level': 'critical'}, {'name': 'sim_pin', 'businessName': 'SIM PIN', 'logicalType': 'string', 'physicalType': 'VARCHAR(8)', 'description': 'SIM PIN if captured in operational support context; should normally not be stored.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'PIN_CODE_SIM', 'is_pii': True, 'pii_category': 'credential', 'sensitivity_level': 'critical'}, {'name': 'puk_code', 'businessName': 'PUK code', 'logicalType': 'string', 'physicalType': 'CHAR(8)', 'description': 'PIN Unlock Key.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'PUK_CODE', 'is_pii': True, 'pii_category': 'credential', 'sensitivity_level': 'critical'}, {'name': 'activation_date', 'businessName': 'Activation date', 'logicalType': 'date', 'physicalType': 'DATE', 'description': 'SIM activation date.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'TIMESTAMP', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'status', 'businessName': 'Subscription status', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Active, suspended, terminated, or ported.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CATEGORY', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'telco_device_and_cpe_inventory': {'businessName': 'Customer device and CPE inventory', 'description': 'Tracks handset, router, ONT, and eSIM/device identifiers associated with subscribers and fixed broadband customers.', 'domain': 'telecom_oss', 'granularity': 'One row per customer-associated device or CPE asset.', 'columns': [{'name': 'device_asset_id', 'businessName': 'Device asset identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal asset key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Customer linked to the device.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'subscription_id', 'businessName': 'Subscription identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Subscription linked to the device.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'SUBSCRIBER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'imei', 'businessName': 'IMEI', 'logicalType': 'string', 'physicalType': 'CHAR(15)', 'description': 'Mobile equipment identifier; validate with Luhn where possible.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IMEI', 'is_pii': True, 'pii_category': 'device_identifier', 'sensitivity_level': 'critical'}, {'name': 'imeisv', 'businessName': 'IMEI software version', 'logicalType': 'string', 'physicalType': 'CHAR(16)', 'description': 'IMEI plus software version.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'IMEISV', 'is_pii': True, 'pii_category': 'device_identifier', 'sensitivity_level': 'high'}, {'name': 'embedded_sim_eid', 'businessName': 'eSIM EID', 'logicalType': 'string', 'physicalType': 'CHAR(32)', 'description': 'Embedded SIM identifier.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EMBEDDED_SIM_EID', 'is_pii': True, 'pii_category': 'telecom_identifier', 'sensitivity_level': 'critical'}, {'name': 'router_serial', 'businessName': 'Router serial number', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Customer premises router serial number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'ROUTER_SERIAL_ALPHANUM', 'is_pii': True, 'pii_category': 'device_identifier', 'sensitivity_level': 'high'}, {'name': 'ont_serial', 'businessName': 'ONT serial number', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'GPON ONT serial number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'ONT_SERIAL', 'is_pii': True, 'pii_category': 'device_identifier', 'sensitivity_level': 'high'}, {'name': 'mac_address', 'businessName': 'MAC address', 'logicalType': 'string', 'physicalType': 'VARCHAR(17)', 'description': 'Device MAC address.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'MAC_ADDRESS', 'is_pii': True, 'pii_category': 'network_identifier', 'sensitivity_level': 'high'}, {'name': 'installation_address_id', 'businessName': 'Installation address identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Reference to fixed-service installation address.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'LOCATION_ADDRESS', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}]}, 'telco_cdr_event': {'businessName': 'Call detail record event', 'description': 'Synthetic call/SMS/data event table for testing PII detection in CDR-style transactional datasets.', 'domain': 'telecom_network', 'granularity': 'One row per call, SMS, or data session event.', 'columns': [{'name': 'cdr_record_id', 'businessName': 'CDR record identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(40)', 'description': 'Unique event identifier from mediation or billing.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'CDR_RECORD_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'event_timestamp', 'businessName': 'Event timestamp', 'logicalType': 'timestamp', 'physicalType': 'TIMESTAMP', 'description': 'Start timestamp for the event.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'TIMESTAMP', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'a_party_msisdn', 'businessName': 'Calling party MSISDN', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Originating mobile number.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'MSISDN', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'b_party_msisdn', 'businessName': 'Called party MSISDN', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Terminating mobile number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'MSISDN', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'imsi', 'businessName': 'IMSI', 'logicalType': 'string', 'physicalType': 'CHAR(15)', 'description': 'Subscriber IMSI observed in event.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IMSI', 'is_pii': True, 'pii_category': 'telecom_identifier', 'sensitivity_level': 'critical'}, {'name': 'imei', 'businessName': 'IMEI', 'logicalType': 'string', 'physicalType': 'CHAR(15)', 'description': 'Device IMEI observed in event.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IMEI', 'is_pii': True, 'pii_category': 'device_identifier', 'sensitivity_level': 'critical'}, {'name': 'cell_global_identity', 'businessName': 'Cell global identity', 'logicalType': 'string', 'physicalType': 'VARCHAR(32)', 'description': 'Serving cell identifier, often linkable to approximate location.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'CELL_GLOBAL_IDENTITY', 'is_pii': True, 'pii_category': 'location_network_identifier', 'sensitivity_level': 'high'}, {'name': 'lac', 'businessName': 'Location area code', 'logicalType': 'integer', 'physicalType': 'INT', 'description': 'Network location area code.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'LAC', 'is_pii': True, 'pii_category': 'location_network_identifier', 'sensitivity_level': 'restricted'}, {'name': 'tac_lte', 'businessName': 'Tracking area code', 'logicalType': 'integer', 'physicalType': 'INT', 'description': 'LTE tracking area code.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'TAC_LTE', 'is_pii': True, 'pii_category': 'location_network_identifier', 'sensitivity_level': 'restricted'}, {'name': 'cgi_latitude', 'businessName': 'Cell latitude', 'logicalType': 'number', 'physicalType': 'DECIMAL(9,6)', 'description': 'Latitude of serving cell or enriched location.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'GPS_LATITUDE', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'cgi_longitude', 'businessName': 'Cell longitude', 'logicalType': 'number', 'physicalType': 'DECIMAL(9,6)', 'description': 'Longitude of serving cell or enriched location.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'GPS_LONGITUDE', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'source_ip', 'businessName': 'Source IP address', 'logicalType': 'string', 'physicalType': 'VARCHAR(45)', 'description': 'Subscriber IP address for data session.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'IP_ADDRESS', 'is_pii': True, 'pii_category': 'network_identifier', 'sensitivity_level': 'high'}, {'name': 'nat_public_ip', 'businessName': 'NAT public IP address', 'logicalType': 'string', 'physicalType': 'VARCHAR(45)', 'description': 'Carrier-grade NAT public IP.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'IPV4_CGNAT', 'is_pii': True, 'pii_category': 'network_identifier', 'sensitivity_level': 'high'}]}, 'telco_billing_invoice': {'businessName': 'Telecom billing and invoice table', 'description': 'Billing-account and invoice data for postpaid, prepaid hybrid, and fixed-line telecom services.', 'domain': 'telecom_billing', 'granularity': 'One row per issued invoice or billing cycle document.', 'columns': [{'name': 'invoice_id', 'businessName': 'Invoice identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal invoice key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'INVOICE_NUMBER', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'billing_account_number', 'businessName': 'Billing account number', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Customer billing account number.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'BILLING_ACCOUNT_NUMBER', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'high'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Customer billed by this invoice.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'bill_to_name', 'businessName': 'Bill-to customer name', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Name printed on invoice.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'bill_to_mobile', 'businessName': 'Bill-to mobile number', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Contact phone on invoice.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EGYPTIAN_MOBILE', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'billing_address', 'businessName': 'Billing address', 'logicalType': 'string', 'physicalType': 'NVARCHAR(300)', 'description': 'Full physical billing address.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'LOCATION_ADDRESS', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'tax_registration_number', 'businessName': 'Tax registration number', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Egyptian TRN for business customer invoices.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'EG_TAX_REGISTRATION_NUMBER', 'is_pii': True, 'pii_category': 'business_identifier', 'sensitivity_level': 'restricted'}, {'name': 'commercial_registry_number', 'businessName': 'Commercial registry number', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Egyptian CRN for business customers.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'EG_COMMERCIAL_REGISTRY_NUMBER', 'is_pii': True, 'pii_category': 'business_identifier', 'sensitivity_level': 'restricted'}, {'name': 'iban', 'businessName': 'Customer IBAN', 'logicalType': 'string', 'physicalType': 'VARCHAR(34)', 'description': 'IBAN used for bank transfer or refund.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IBAN_CODE', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'card_last4', 'businessName': 'Card last four digits', 'logicalType': 'string', 'physicalType': 'CHAR(4)', 'description': 'Masked payment-card suffix.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CREDIT_CARD', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'restricted'}, {'name': 'invoice_amount_egp', 'businessName': 'Invoice amount in EGP', 'logicalType': 'number', 'physicalType': 'DECIMAL(12,2)', 'description': 'Invoice amount.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'eshop_customer_account': {'businessName': 'E-commerce customer account', 'description': 'Customer account table for Egyptian e-commerce, marketplace, and digital retail use cases.', 'domain': 'ecommerce_crm', 'granularity': 'One row per registered customer account.', 'columns': [{'name': 'eshop_customer_id', 'businessName': 'E-shop customer ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal customer account key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'full_name', 'businessName': 'Full customer name', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Displayed or legal customer name.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'email_address', 'businessName': 'Email address', 'logicalType': 'string', 'physicalType': 'VARCHAR(254)', 'description': 'Login and notification email.', 'required': True, 'unique': True, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EMAIL_ADDRESS', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'mobile_number', 'businessName': 'Mobile number', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Egyptian mobile used for OTP, delivery, and support.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EGYPTIAN_MOBILE', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'password_hash', 'businessName': 'Password hash', 'logicalType': 'string', 'physicalType': 'VARCHAR(255)', 'description': 'Password hash; classify as secret even when not reversible.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'HASH_BCRYPT', 'is_pii': True, 'pii_category': 'credential', 'sensitivity_level': 'critical'}, {'name': 'session_id', 'businessName': 'Session identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(128)', 'description': 'Active or historical session identifier.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'SESSION_ID_ALPHANUM', 'is_pii': True, 'pii_category': 'credential', 'sensitivity_level': 'critical'}, {'name': 'csrf_token', 'businessName': 'CSRF token', 'logicalType': 'string', 'physicalType': 'VARCHAR(128)', 'description': 'Web anti-forgery token.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'CSRF_TOKEN', 'is_pii': True, 'pii_category': 'credential', 'sensitivity_level': 'critical'}, {'name': 'social_profile_url', 'businessName': 'Social login profile URL', 'logicalType': 'string', 'physicalType': 'VARCHAR(255)', 'description': 'Linked social profile URL where collected.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'FACEBOOK_PROFILE_URL', 'is_pii': True, 'pii_category': 'online_identifier', 'sensitivity_level': 'high'}, {'name': 'account_created_at', 'businessName': 'Account creation timestamp', 'logicalType': 'timestamp', 'physicalType': 'TIMESTAMP', 'description': 'Account creation timestamp.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'TIMESTAMP', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'eshop_shipping_address': {'businessName': 'E-commerce shipping address book', 'description': 'Address-book table containing Egyptian delivery-address components needed for e-commerce fulfillment.', 'domain': 'ecommerce_fulfillment', 'granularity': 'One row per saved customer shipping address.', 'columns': [{'name': 'address_id', 'businessName': 'Address identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal address key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'eshop_customer_id', 'businessName': 'E-shop customer ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Customer who owns this address.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'recipient_name', 'businessName': 'Recipient name', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Name of recipient at delivery location.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'recipient_mobile', 'businessName': 'Recipient mobile', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Mobile used by courier for delivery.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EGYPTIAN_MOBILE', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'governorate', 'businessName': 'Governorate', 'logicalType': 'string', 'physicalType': 'VARCHAR(60)', 'description': 'Egyptian governorate such as Cairo, Giza, Alexandria.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'GOVERNORATE', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'restricted'}, {'name': 'city', 'businessName': 'City', 'logicalType': 'string', 'physicalType': 'VARCHAR(80)', 'description': 'City or town.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'LOCATION_ADDRESS', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'restricted'}, {'name': 'district', 'businessName': 'District or neighborhood', 'logicalType': 'string', 'physicalType': 'NVARCHAR(120)', 'description': 'District, area, or neighborhood.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EG_DISTRICT', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'street_address', 'businessName': 'Street address', 'logicalType': 'string', 'physicalType': 'NVARCHAR(240)', 'description': 'Street name and house/building number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'LOCATION_ADDRESS', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'building_floor_apartment', 'businessName': 'Building floor and apartment', 'logicalType': 'string', 'physicalType': 'NVARCHAR(120)', 'description': 'Building, floor, apartment, landmark, or unit details.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EG_BUILDING_DETAILS', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'postal_code', 'businessName': 'Postal code', 'logicalType': 'string', 'physicalType': 'CHAR(5)', 'description': 'Egyptian five-digit postal code.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'EG_POSTAL_CODE', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'restricted'}, {'name': 'gps_pair', 'businessName': 'Delivery GPS coordinates', 'logicalType': 'string', 'physicalType': 'VARCHAR(50)', 'description': 'Optional GPS coordinates captured by courier or app.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'GPS_PAIR', 'is_pii': True, 'pii_category': 'precise_location', 'sensitivity_level': 'critical'}, {'name': 'what3words', 'businessName': 'What3Words address', 'logicalType': 'string', 'physicalType': 'VARCHAR(80)', 'description': 'Optional what3words precise address.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'WHAT3WORDS', 'is_pii': True, 'pii_category': 'precise_location', 'sensitivity_level': 'critical'}]}, 'eshop_order_header': {'businessName': 'E-commerce order header', 'description': 'Order-level table connecting customers, delivery address, payment channel, and fulfillment status.', 'domain': 'ecommerce_orders', 'granularity': 'One row per placed order.', 'columns': [{'name': 'order_id', 'businessName': 'Order identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Order key visible to customer and support.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'eshop_customer_id', 'businessName': 'E-shop customer ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Customer placing the order.', 'required': True, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'recipient_name', 'businessName': 'Recipient name snapshot', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Recipient name copied from address at order time.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'recipient_mobile', 'businessName': 'Recipient mobile snapshot', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Delivery contact mobile.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EGYPTIAN_MOBILE', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'shipping_address_text', 'businessName': 'Shipping address snapshot', 'logicalType': 'string', 'physicalType': 'NVARCHAR(500)', 'description': 'Full delivery address at order time.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'LOCATION_ADDRESS', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'shipping_tracking_number', 'businessName': 'Shipping tracking number', 'logicalType': 'string', 'physicalType': 'VARCHAR(60)', 'description': 'Carrier tracking number shared with logistics provider.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'SHIPPING_TRACKING_NUMBER', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'payment_method', 'businessName': 'Payment method', 'logicalType': 'string', 'physicalType': 'VARCHAR(40)', 'description': 'Cash on delivery, card, Meeza, mobile wallet, Fawry, InstaPay, or BNPL.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CATEGORY', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'order_total_egp', 'businessName': 'Order total EGP', 'logicalType': 'number', 'physicalType': 'DECIMAL(12,2)', 'description': 'Total payable order amount.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'created_at', 'businessName': 'Order creation timestamp', 'logicalType': 'timestamp', 'physicalType': 'TIMESTAMP', 'description': 'Order creation timestamp.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'TIMESTAMP', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'fintech_payment_transaction': {'businessName': 'Egypt fintech payment transaction', 'description': 'Unified payment-transaction table covering card, Meeza, Fawry, mobile-wallet, InstaPay, and bank-transfer flows used by telecom and e-commerce.', 'domain': 'fintech_payments', 'granularity': 'One row per payment authorization, capture, transfer, or settlement transaction.', 'columns': [{'name': 'payment_transaction_id', 'businessName': 'Payment transaction ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(64)', 'description': 'Payment gateway transaction key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'STRIPE_PAYMENT_INTENT', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'restricted'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Customer associated with payment.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'payer_name', 'businessName': 'Payer name', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Name of payer when captured.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'payer_mobile', 'businessName': 'Payer mobile', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Mobile number linked to wallet or payer contact.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_MOBILE_WALLET_NUMBER', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'instapay_address', 'businessName': 'InstaPay address', 'logicalType': 'string', 'physicalType': 'VARCHAR(80)', 'description': 'InstaPay payment address or IPA.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'INSTAPAY_ADDRESS', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'fawry_reference', 'businessName': 'Fawry reference number', 'logicalType': 'string', 'physicalType': 'VARCHAR(40)', 'description': 'Fawry payment reference used for cash collection or bill payment.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_FAWRY_REFERENCE', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'meeza_card_pan', 'businessName': 'Meeza card PAN', 'logicalType': 'string', 'physicalType': 'VARCHAR(19)', 'description': 'Synthetic Meeza card number; never include real PANs in golden data.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_MEEZA_CARD', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'card_pan', 'businessName': 'Payment card PAN', 'logicalType': 'string', 'physicalType': 'VARCHAR(19)', 'description': 'Card primary account number; use tokenized or synthetic values only.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'CREDIT_CARD', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'card_expiry', 'businessName': 'Card expiry', 'logicalType': 'string', 'physicalType': 'VARCHAR(7)', 'description': 'Card expiration date.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'CARD_EXPIRY', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'cvv', 'businessName': 'Card verification value', 'logicalType': 'string', 'physicalType': 'VARCHAR(4)', 'description': 'Card CVV; should not be stored after authorization.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'CVV', 'is_pii': True, 'pii_category': 'credential_financial', 'sensitivity_level': 'critical'}, {'name': 'iban', 'businessName': 'IBAN', 'logicalType': 'string', 'physicalType': 'VARCHAR(34)', 'description': 'Bank account IBAN used for bank transfer or refund.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IBAN_CODE', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'bank_account_number', 'businessName': 'Local bank account number', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Egyptian or generic bank account number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_BANK_ACCOUNT', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'swift_bic', 'businessName': 'SWIFT/BIC', 'logicalType': 'string', 'physicalType': 'VARCHAR(11)', 'description': 'Bank routing identifier.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'SWIFT_BIC_EGYPT', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'restricted'}, {'name': 'amount_egp', 'businessName': 'Amount in EGP', 'logicalType': 'number', 'physicalType': 'DECIMAL(12,2)', 'description': 'Transaction amount.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'fintech_mobile_wallet_account': {'businessName': 'Egypt mobile wallet account', 'description': 'Mobile wallet account table for Vodafone Cash, Orange Cash, Etisalat Cash, WE Pay, and similar Egyptian wallet products.', 'domain': 'fintech_wallets', 'granularity': 'One row per mobile wallet account.', 'columns': [{'name': 'wallet_account_id', 'businessName': 'Wallet account ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal wallet account key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'wallet_msisdn', 'businessName': 'Wallet mobile number', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Mobile number that identifies the wallet account.', 'required': True, 'unique': True, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_MOBILE_WALLET_NUMBER', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Linked customer profile.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'eg_national_id', 'businessName': 'Egyptian national ID', 'logicalType': 'string', 'physicalType': 'CHAR(14)', 'description': 'KYC national ID linked to wallet.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_NATIONAL_ID', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'critical'}, {'name': 'kyc_full_name', 'businessName': 'KYC full name', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Legal name verified for wallet.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'wallet_provider', 'businessName': 'Wallet provider', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Wallet brand or provider.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CATEGORY', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'wallet_pin_hash', 'businessName': 'Wallet PIN hash', 'logicalType': 'string', 'physicalType': 'VARCHAR(255)', 'description': 'Hash of wallet PIN if held by platform.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'HASH_BCRYPT', 'is_pii': True, 'pii_category': 'credential', 'sensitivity_level': 'critical'}, {'name': 'last_login_ip', 'businessName': 'Last login IP address', 'logicalType': 'string', 'physicalType': 'VARCHAR(45)', 'description': 'Last IP address used for wallet access.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'IP_ADDRESS', 'is_pii': True, 'pii_category': 'network_identifier', 'sensitivity_level': 'high'}, {'name': 'device_imei', 'businessName': 'Registered device IMEI', 'logicalType': 'string', 'physicalType': 'CHAR(15)', 'description': 'Device associated with wallet app.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IMEI', 'is_pii': True, 'pii_category': 'device_identifier', 'sensitivity_level': 'critical'}, {'name': 'status', 'businessName': 'Wallet status', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Active, suspended, closed, or restricted.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CATEGORY', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'merchant_seller_registry': {'businessName': 'Marketplace merchant and seller registry', 'description': 'Business seller registry for Egyptian e-commerce marketplaces, covering tax, commercial, settlement, and contact identifiers.', 'domain': 'ecommerce_merchant', 'granularity': 'One row per merchant legal entity or sole proprietor seller.', 'columns': [{'name': 'merchant_id', 'businessName': 'Merchant identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal seller or merchant key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'merchant_legal_name', 'businessName': 'Merchant legal name', 'logicalType': 'string', 'physicalType': 'NVARCHAR(200)', 'description': 'Registered business or sole proprietor legal name.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'merchant_contact_name', 'businessName': 'Merchant contact person', 'logicalType': 'string', 'physicalType': 'NVARCHAR(160)', 'description': 'Operational contact person.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'PERSON', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'merchant_mobile', 'businessName': 'Merchant mobile', 'logicalType': 'string', 'physicalType': 'VARCHAR(16)', 'description': 'Mobile number for merchant contact and OTP.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EGYPTIAN_MOBILE', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'merchant_email', 'businessName': 'Merchant email', 'logicalType': 'string', 'physicalType': 'VARCHAR(254)', 'description': 'Merchant contact email.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'EMAIL_ADDRESS', 'is_pii': True, 'pii_category': 'direct_identifier', 'sensitivity_level': 'high'}, {'name': 'tax_registration_number', 'businessName': 'Tax registration number', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Egyptian tax registration number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'EG_TAX_REGISTRATION_NUMBER', 'is_pii': True, 'pii_category': 'business_identifier', 'sensitivity_level': 'restricted'}, {'name': 'commercial_registry_number', 'businessName': 'Commercial registry number', 'logicalType': 'string', 'physicalType': 'VARCHAR(20)', 'description': 'Egyptian commercial registry number.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'EG_COMMERCIAL_REGISTRY_NUMBER', 'is_pii': True, 'pii_category': 'business_identifier', 'sensitivity_level': 'restricted'}, {'name': 'unified_national_number', 'businessName': 'Unified national number', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Unified establishment number where available.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'EG_UNIFIED_NATIONAL_NUMBER', 'is_pii': True, 'pii_category': 'business_identifier', 'sensitivity_level': 'restricted'}, {'name': 'settlement_iban', 'businessName': 'Settlement IBAN', 'logicalType': 'string', 'physicalType': 'VARCHAR(34)', 'description': 'Bank account IBAN for merchant settlement.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'IBAN_CODE', 'is_pii': True, 'pii_category': 'financial_identifier', 'sensitivity_level': 'critical'}, {'name': 'business_address', 'businessName': 'Business address', 'logicalType': 'string', 'physicalType': 'NVARCHAR(300)', 'description': 'Registered business address.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'high', 'semantic_type': 'LOCATION_ADDRESS', 'is_pii': True, 'pii_category': 'location', 'sensitivity_level': 'high'}, {'name': 'national_real_estate_id', 'businessName': 'National real estate ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(40)', 'description': 'Property identifier for business premises where available.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'EG_NATIONAL_REAL_ESTATE_ID', 'is_pii': True, 'pii_category': 'location_identifier', 'sensitivity_level': 'restricted'}]}, 'support_ticket_free_text': {'businessName': 'Support ticket and free-text interaction log', 'description': 'Free-text ticket data for evaluating entity extraction from notes, SMS bodies, chat transcripts, emails, and logs.', 'domain': 'cross_domain_support', 'granularity': 'One row per support interaction or message.', 'columns': [{'name': 'ticket_id', 'businessName': 'Ticket identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Support ticket identifier.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Customer linked to ticket.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'channel', 'businessName': 'Interaction channel', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Call center, chat, email, WhatsApp, SMS, app, or branch.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CATEGORY', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'agent_notes', 'businessName': 'Agent free-text notes', 'logicalType': 'string', 'physicalType': 'NVARCHAR(4000)', 'description': 'Free text may contain phone, national ID, email, address, OTP, card, IBAN, IMEI, IMSI, ICCID, or passport values.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'FREE_TEXT', 'is_pii': True, 'pii_category': 'mixed_unstructured_pii', 'sensitivity_level': 'critical'}, {'name': 'sms_body', 'businessName': 'SMS body', 'logicalType': 'string', 'physicalType': 'NVARCHAR(500)', 'description': 'SMS content for OTP and notification detection.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'FREE_TEXT', 'is_pii': True, 'pii_category': 'mixed_unstructured_pii', 'sensitivity_level': 'critical'}, {'name': 'attachment_text', 'businessName': 'Extracted attachment text', 'logicalType': 'string', 'physicalType': 'NVARCHAR(4000)', 'description': 'OCR or extracted attachment text, often containing ID documents or invoices.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'FREE_TEXT', 'is_pii': True, 'pii_category': 'mixed_unstructured_pii', 'sensitivity_level': 'critical'}, {'name': 'detected_language', 'businessName': 'Detected language', 'logicalType': 'string', 'physicalType': 'VARCHAR(10)', 'description': 'Language code for text-processing evaluation.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'CATEGORY', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'created_at', 'businessName': 'Ticket creation timestamp', 'logicalType': 'timestamp', 'physicalType': 'TIMESTAMP', 'description': 'Ticket timestamp.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'internal', 'semantic_type': 'TIMESTAMP', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}]}, 'sensitive_customer_risk_profile': {'businessName': 'Sensitive customer risk and eligibility profile', 'description': 'Synthetic table for high-risk sensitive personal data classes under privacy and sector controls. Use only synthetic values.', 'domain': 'cross_domain_sensitive', 'granularity': 'One row per customer risk or eligibility assessment snapshot.', 'columns': [{'name': 'risk_profile_id', 'businessName': 'Risk profile ID', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Internal risk profile key.', 'required': True, 'unique': True, 'primaryKey': True, 'classification': 'internal', 'semantic_type': 'NUMERIC_ID', 'is_pii': False, 'pii_category': 'non_pii', 'sensitivity_level': 'internal'}, {'name': 'customer_id', 'businessName': 'Customer identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(36)', 'description': 'Linked customer.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'restricted', 'semantic_type': 'CUSTOMER_ID', 'is_pii': True, 'pii_category': 'linkable_identifier', 'sensitivity_level': 'restricted'}, {'name': 'criminal_record_reference', 'businessName': 'Criminal record reference', 'logicalType': 'string', 'physicalType': 'VARCHAR(60)', 'description': 'Police record or criminal-status reference where legally collected.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'EG_CRIMINAL_RECORD', 'is_pii': True, 'pii_category': 'sensitive_personal_data', 'sensitivity_level': 'critical'}, {'name': 'health_insurance_id', 'businessName': 'Health insurance identifier', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Health insurance or medical benefit member ID.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'HEALTH_DATA', 'is_pii': True, 'pii_category': 'sensitive_personal_data', 'sensitivity_level': 'critical'}, {'name': 'medical_record_number', 'businessName': 'Medical record number', 'logicalType': 'string', 'physicalType': 'VARCHAR(30)', 'description': 'Medical record number in partner add-on services.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'MEDICAL_RECORD_NUMBER', 'is_pii': True, 'pii_category': 'sensitive_personal_data', 'sensitivity_level': 'critical'}, {'name': 'biometric_template_ref', 'businessName': 'Biometric template reference', 'logicalType': 'string', 'physicalType': 'VARCHAR(256)', 'description': 'Reference to biometric template, fingerprint, face, or liveness artifact.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'BIOMETRIC_DATA', 'is_pii': True, 'pii_category': 'sensitive_personal_data', 'sensitivity_level': 'critical'}, {'name': 'religion', 'businessName': 'Religion', 'logicalType': 'string', 'physicalType': 'VARCHAR(50)', 'description': 'Religion where collected for lawful personal-status context; avoid unless necessary.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'RELIGION', 'is_pii': True, 'pii_category': 'sensitive_personal_data', 'sensitivity_level': 'critical'}, {'name': 'political_affiliation', 'businessName': 'Political affiliation', 'logicalType': 'string', 'physicalType': 'VARCHAR(100)', 'description': 'Political view or affiliation if present in unstructured/third-party data.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'NRP', 'is_pii': True, 'pii_category': 'sensitive_personal_data', 'sensitivity_level': 'critical'}, {'name': 'children_data_flag', 'businessName': 'Children data indicator', 'logicalType': 'boolean', 'physicalType': 'BOOLEAN', 'description': 'Indicates whether data subject is a child/minor.', 'required': False, 'unique': False, 'primaryKey': False, 'classification': 'critical', 'semantic_type': 'CHILDREN_DATA', 'is_pii': True, 'pii_category': 'sensitive_personal_data', 'sensitivity_level': 'critical'}]}}

FIRST_NAMES_EN = ["Ahmed", "Mohamed", "Mahmoud", "Omar", "Youssef", "Mariam", "Nour", "Salma", "Hana", "Farida"]
FATHER_NAMES_EN = ["Hassan", "Ali", "Ibrahim", "Mostafa", "Samir", "Khaled", "Tarek", "Adel", "Nabil", "Fouad"]
FAMILY_NAMES_EN = ["ElSayed", "Abdelrahman", "Mahfouz", "Shoukry", "Fathy", "Gaber", "Zaki", "Kamel", "Nassar", "Darwish"]
FIRST_NAMES_AR = ["أحمد", "محمد", "محمود", "عمر", "يوسف", "مريم", "نور", "سلمى", "هنا", "فريدة"]
FATHER_NAMES_AR = ["حسن", "علي", "إبراهيم", "مصطفى", "سمير", "خالد", "طارق", "عادل", "نبيل", "فؤاد"]
FAMILY_NAMES_AR = ["السيد", "عبد الرحمن", "محفوظ", "شكري", "فتحي", "جابر", "زكي", "كامل", "نصار", "درويش"]
GOVERNORATES = ["Cairo", "Giza", "Alexandria", "Dakahlia", "Sharqia", "Qalyubia", "Gharbia", "Monufia", "Beheira", "Suez", "Port Said", "Ismailia", "Minya", "Asyut", "Sohag", "Qena", "Luxor", "Aswan"]
DISTRICTS = ["Nasr City", "Heliopolis", "Maadi", "Dokki", "Mohandessin", "6th of October", "New Cairo", "Smouha", "Mansoura", "Zagazig"]
STREETS = ["Tahrir Square", "Makram Ebeid", "Abbas El Akkad", "El Haram", "Corniche El Nile", "Gameat El Dowal", "Mostafa El Nahas", "Salah Salem"]
EMAIL_DOMAINS = ["example.com", "mail.example", "test.eg", "shop.example"]
WALLET_PROVIDERS = ["Vodafone Cash", "Orange Cash", "Etisalat Cash", "WE Pay"]
PAYMENT_METHODS = ["cash_on_delivery", "card", "meeza", "mobile_wallet", "fawry", "instapay", "bank_transfer", "bnpl"]
CHANNELS = ["call_center", "chat", "email", "whatsapp", "sms", "mobile_app", "branch"]
STATUSES = ["active", "suspended", "terminated", "closed", "pending", "verified"]


def _rng(seed: int, table_name: str) -> random.Random:
    table_hash = int(hashlib.sha256(table_name.encode("utf-8")).hexdigest()[:8], 16)
    return random.Random(seed + table_hash)


def _digits(rng: random.Random, length: int) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(length))


def _alnum(rng: random.Random, length: int) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(rng.choice(alphabet) for _ in range(length))


def _hex(rng: random.Random, length: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(length))


def _full_name_en(i: int) -> str:
    return f"{FIRST_NAMES_EN[i % len(FIRST_NAMES_EN)]} {FATHER_NAMES_EN[(i * 3) % len(FATHER_NAMES_EN)]} {FAMILY_NAMES_EN[(i * 7) % len(FAMILY_NAMES_EN)]}"


def _full_name_ar(i: int) -> str:
    return f"{FIRST_NAMES_AR[i % len(FIRST_NAMES_AR)]} {FATHER_NAMES_AR[(i * 3) % len(FATHER_NAMES_AR)]} {FAMILY_NAMES_AR[(i * 7) % len(FAMILY_NAMES_AR)]}"


def _slug_name(i: int) -> str:
    return re.sub(r"[^a-z0-9]+", ".", _full_name_en(i).lower()).strip(".")


def _egypt_mobile(i: int, e164: bool = True) -> str:
    prefixes = ["10", "11", "12", "15"]
    local10 = prefixes[i % len(prefixes)] + f"{10000000 + (i * 7919) % 90000000:08d}"
    return "+20" + local10 if e164 else "0" + local10


def _landline(i: int) -> str:
    area = ["02", "03", "040", "050", "055", "062", "082", "088", "097"][i % 9]
    digits_needed = 10 - len(area)
    return area + f"{1000000 + (i * 3571) % 9000000:0{digits_needed}d}"[-digits_needed:]


def _email(i: int) -> str:
    return f"{_slug_name(i)}{i:04d}@{EMAIL_DOMAINS[i % len(EMAIL_DOMAINS)]}"


def _date_of_birth(i: int) -> date:
    return date(1965 + (i % 40), 1 + (i % 12), 1 + (i % 27))


def _timestamp(i: int) -> datetime:
    return datetime(2024, 1, 1, 9, 0, 0) + timedelta(minutes=i * 11)


def _egypt_national_id(i: int) -> str:
    dob = _date_of_birth(i)
    century = "2" if dob.year < 2000 else "3"
    yy = dob.year % 100
    governorate_codes = ["01", "02", "03", "04", "11", "12", "13", "14", "15", "16", "17", "18", "19", "21", "22", "23", "24", "25", "26", "27", "28", "29", "31", "32", "33", "34", "35", "88"]
    gov = governorate_codes[i % len(governorate_codes)]
    seq = f"{1000 + (i * 17) % 9000:04d}"
    check = str((i * 7 + 3) % 10)
    return f"{century}{yy:02d}{dob.month:02d}{dob.day:02d}{gov}{seq}{check}"


def _imei(i: int) -> str:
    # Synthetic 15-digit device identifier; not guaranteed to pass Luhn.
    return f"49{(154203237000 + i) % 10**13:013d}"[-15:]


def _imsi(i: int) -> str:
    # 602 is Egypt MCC; synthetic MNC/subscriber suffix follows.
    return "602" + ["01", "02", "03", "04"][i % 4] + f"{1000000000 + i:010d}"[-10:]


def _iccid(i: int) -> str:
    return "8920" + f"{1000000000000000 + i:016d}"


def _address(i: int) -> str:
    return f"{12 + i % 80} {STREETS[i % len(STREETS)]}, {DISTRICTS[i % len(DISTRICTS)]}, {GOVERNORATES[i % len(GOVERNORATES)]}"


def _gps_pair(i: int) -> str:
    lat = 30.0444 + ((i % 100) - 50) / 10000
    lon = 31.2357 + ((i % 100) - 50) / 10000
    return f"{lat:.6f}, {lon:.6f}"


def _iban(i: int) -> str:
    # Synthetic Egypt-like IBAN shape for testing only.
    return "EG" + f"{38 + i % 61:02d}" + f"{19 + i % 80:04d}" + f"{5000000000263180000 + i:019d}"[:23]


def _hash_value(i: int, prefix: str = "") -> str:
    return prefix + hashlib.sha256(f"synthetic-secret-{i}".encode()).hexdigest()


def _semantic_value(table_name: str, col: Dict[str, Any], i: int, rng: random.Random) -> Any:
    name = col["name"].lower()
    sem = col.get("semantic_type", "NON_PII")
    logical = col.get("logicalType", "string")

    if logical == "boolean" or name.endswith("_flag"):
        return bool(i % 2)
    if logical == "date":
        return _date_of_birth(i) if "birth" in name else date(2024, 1, 1) + timedelta(days=i % 365)
    if logical == "timestamp":
        return _timestamp(i)
    if logical in {"number", "integer"}:
        if "amount" in name or "total" in name:
            return round(50.0 + (i % 5000) * 1.25, 2)
        return int(1000 + i)

    if name.endswith("_id") or name in {"customer_id", "subscription_id", "order_id", "merchant_id", "wallet_account_id", "risk_profile_id", "address_id", "ticket_id", "payment_transaction_id"}:
        prefixes = {
            "telco_customer_profile": "CUST-EG",
            "telco_kyc_identity_document": "KYC-EG",
            "telco_subscriber_sim_registry": "SUB",
            "telco_device_and_cpe_inventory": "DEV",
            "telco_cdr_event": "CDR",
            "telco_billing_invoice": "INV-ID",
            "eshop_customer_account": "ESH-CUST",
            "eshop_shipping_address": "ADDR",
            "eshop_order_header": "ORD",
            "fintech_payment_transaction": "PAY",
            "fintech_mobile_wallet_account": "WALLET",
            "merchant_seller_registry": "MERCH",
            "support_ticket_free_text": "TCKT",
            "sensitive_customer_risk_profile": "RISK",
        }
        return f"{prefixes.get(table_name, 'ID')}-{i+1:08d}"

    if sem in {"PERSON"} or "name" in name:
        if name.endswith("_ar") or "arabic" in name:
            return _full_name_ar(i)
        return _full_name_en(i)
    if sem in {"EGYPTIAN_MOBILE", "MSISDN", "EG_MOBILE_WALLET_NUMBER"} or "mobile" in name or "msisdn" in name:
        return _egypt_mobile(i, True)
    if sem == "PHONE_NUMBER" or "phone" in name:
        return _landline(i) if i % 3 == 0 else _egypt_mobile(i, False)
    if sem == "EMAIL_ADDRESS" or "email" in name:
        return _email(i)
    if sem == "DATE_OF_BIRTH":
        return _date_of_birth(i)
    if sem == "GENDER_INDICATOR" or name == "gender":
        return ["Male", "Female"][i % 2]
    if sem == "NRP" or "nationality" in name:
        return ["EGY", "SYR", "JOR", "SDN", "SAU"][i % 5]
    if sem == "EG_NATIONAL_ID":
        return _egypt_national_id(i)
    if sem == "EG_PASSPORT" or "passport" in name:
        return chr(65 + i % 26) + f"{10000000 + i:08d}"[-8:]
    if sem in {"EG_DRIVER_LICENSE", "EG_RESIDENCY_PERMIT", "EG_MILITARY_ID", "EG_SYNDICATE_ID"}:
        return f"{sem.split('_')[-2][:3] if '_' in sem else 'DOC'}-{100000 + i}"
    if sem == "BIOMETRIC_DATA" or "image" in name or "template" in name:
        return f"s3://synthetic-pii-artifacts/{table_name}/{i+1:08d}.bin"
    if sem == "IMSI":
        return _imsi(i)
    if sem == "ICCID":
        return _iccid(i)
    if sem in {"IMEI", "IMEISV"}:
        return _imei(i) + ("01" if sem == "IMEISV" else "")
    if sem == "PIN_CODE_SIM":
        return f"{1000 + i % 9000:04d}"
    if sem == "PUK_CODE":
        return f"{10000000 + i % 90000000:08d}"
    if sem in {"ROUTER_SERIAL_ALPHANUM", "ONT_SERIAL"}:
        return ("ONT" if sem == "ONT_SERIAL" else "RTR") + _alnum(rng, 12)
    if sem == "MAC_ADDRESS":
        return ":".join(_hex(rng, 2).upper() for _ in range(6))
    if sem in {"CELL_GLOBAL_IDENTITY", "LAC", "TAC_LTE"}:
        return f"602-{1 + i % 4:02d}-{100 + i % 60000}-{1000 + i % 50000}" if sem == "CELL_GLOBAL_IDENTITY" else str(1000 + i % 60000)
    if sem in {"GPS_LATITUDE", "GPS_LONGITUDE"}:
        pair = _gps_pair(i).split(", ")
        return float(pair[0] if sem == "GPS_LATITUDE" else pair[1])
    if sem == "GPS_PAIR":
        return _gps_pair(i)
    if sem == "IP_ADDRESS" or "ip" in name:
        return f"100.{64 + i % 64}.{(i // 256) % 256}.{i % 256}"
    if sem == "IPV4_CGNAT":
        return f"100.{64 + i % 64}.{(i * 3) % 256}.{(i * 7) % 256}"
    if sem in {"BILLING_ACCOUNT_NUMBER", "INVOICE_NUMBER"} or "invoice" in name:
        return f"INV-{202600000 + i}" if "invoice" in name else f"ACCT-{i+1:08d}"
    if sem in {"LOCATION_ADDRESS", "EG_BUILDING_DETAILS", "EG_DISTRICT", "GOVERNORATE", "EG_POSTAL_CODE", "WHAT3WORDS"} or "address" in name or name in {"city", "district", "governorate", "postal_code"}:
        if name == "governorate":
            return GOVERNORATES[i % len(GOVERNORATES)]
        if name == "city":
            return ["Cairo", "Giza", "Alexandria", "Mansoura", "Tanta", "Asyut"][i % 6]
        if name == "district":
            return DISTRICTS[i % len(DISTRICTS)]
        if name == "postal_code":
            return f"{11000 + i % 8999:05d}"
        if sem == "WHAT3WORDS":
            return f"delta.market.{['cairo','giza','nile','tower','square'][i%5]}"
        if sem == "EG_BUILDING_DETAILS":
            return f"Building {10+i%90}, Floor {1+i%12}, Apartment {1+i%24}"
        return _address(i)
    if sem in {"EG_TAX_REGISTRATION_NUMBER", "EG_COMMERCIAL_REGISTRY_NUMBER", "EG_UNIFIED_NATIONAL_NUMBER", "EG_NATIONAL_REAL_ESTATE_ID"}:
        return f"{100 + i % 900}-{100 + (i*3) % 900}-{100 + (i*7) % 900}"
    if sem in {"IBAN_CODE", "EG_BANK_ACCOUNT"} or "iban" in name or "bank_account" in name:
        return _iban(i) if "iban" in name or sem == "IBAN_CODE" else f"{100000000000 + i:012d}"
    if sem == "CREDIT_CARD" or "pan" in name or "card" in name:
        return "627033" + f"{1000000000 + i:010d}" if "meeza" in name else "411111" + f"{1000000000 + i:010d}"
    if sem == "CVV" or name == "cvv":
        return f"{100 + i % 900:03d}"
    if sem == "CARD_EXPIRY":
        return f"{1 + i % 12:02d}/{26 + i % 8}"
    if sem == "INSTAPAY_ADDRESS":
        return f"{_slug_name(i)}{i:03d}@instapay"
    if sem == "EG_FAWRY_REFERENCE":
        return f"FWY{202600000000 + i}"
    if sem == "SWIFT_BIC_EGYPT":
        return ["CIBEEGCX", "BMISEGCX", "NBEGEGCX", "EGBAEGCX"][i % 4]
    if sem in {"HASH_BCRYPT", "SESSION_ID_ALPHANUM", "CSRF_TOKEN"} or "hash" in name or "token" in name or "session" in name:
        return _hash_value(i, "$2b$12$")[:60] if "hash" in name else _hash_value(i)[:32]
    if sem in {"FACEBOOK_PROFILE_URL", "LINKEDIN_PROFILE_URL"} or "profile" in name:
        return f"https://facebook.com/{_slug_name(i)}"
    if sem == "SHIPPING_TRACKING_NUMBER" or "tracking" in name:
        return f"EG-TRK-{2026000000 + i}"
    if sem == "FREE_TEXT" or "notes" in name or "body" in name or "text" in name:
        if "sms" in name:
            return f"Your OTP is {100000 + i % 900000} for login to your account. Do not share it."
        return f"Customer {_egypt_mobile(i)} confirmed NID {_egypt_national_id(i)} and requested update for IMEI {_imei(i)}."
    if sem in {"MEDICAL_RECORD_NUMBER", "HEALTH_DATA"}:
        return f"MRN-{100000 + i}"
    if sem in {"EG_CRIMINAL_RECORD", "RELIGION", "CHILDREN_DATA"}:
        if sem == "RELIGION":
            return ["Muslim", "Christian", "Not Collected"][i % 3]
        if sem == "CHILDREN_DATA":
            return bool(i % 5 == 0)
        return f"PCR-{100000 + i}"
    if sem == "CATEGORY" or name in {"status", "channel", "payment_method", "wallet_provider", "operator_original_prefix"}:
        if name == "status":
            return STATUSES[i % len(STATUSES)]
        if name == "channel":
            return CHANNELS[i % len(CHANNELS)]
        if name == "payment_method":
            return PAYMENT_METHODS[i % len(PAYMENT_METHODS)]
        if name == "wallet_provider":
            return WALLET_PROVIDERS[i % len(WALLET_PROVIDERS)]
        if name == "operator_original_prefix":
            return ["Vodafone", "Etisalat (e&)", "Orange", "WE"][i % 4]
        return "synthetic_category"
    return f"{name}_{i+1:08d}"


def _spark_type(logical_type: str):
    from pyspark.sql.types import BooleanType, DateType, DoubleType, IntegerType, StringType, TimestampType
    return {
        "boolean": BooleanType(),
        "date": DateType(),
        "timestamp": TimestampType(),
        "number": DoubleType(),
        "integer": IntegerType(),
        "string": StringType(),
    }.get(logical_type or "string", StringType())


def get_table_schema(table_name: str):
    """Return a PySpark ``StructType`` for one configured table."""
    from pyspark.sql.types import StructField, StructType
    if table_name not in TABLE_SCHEMAS:
        raise KeyError(f"Unknown table_name={table_name!r}. Expected one of: {sorted(TABLE_SCHEMAS)}")
    fields = []
    for c in TABLE_SCHEMAS[table_name]["columns"]:
        fields.append(StructField(c["name"], _spark_type(c.get("logicalType", "string")), nullable=not bool(c.get("required"))))
    return StructType(fields)


def get_table_columns(table_name: str) -> List[str]:
    """Return the expected column order for one configured table."""
    if table_name not in TABLE_SCHEMAS:
        raise KeyError(f"Unknown table_name={table_name!r}. Expected one of: {sorted(TABLE_SCHEMAS)}")
    return [c["name"] for c in TABLE_SCHEMAS[table_name]["columns"]]


def describe_table(table_name: str) -> Dict[str, Any]:
    """Return table metadata, including semantic type and PII metadata for every column."""
    if table_name not in TABLE_SCHEMAS:
        raise KeyError(f"Unknown table_name={table_name!r}. Expected one of: {sorted(TABLE_SCHEMAS)}")
    return TABLE_SCHEMAS[table_name]


def _generate_table(spark: Any, table_name: str, num_rows: int, seed: int = 42):
    if num_rows < 0:
        raise ValueError("num_rows must be >= 0")
    rng = _rng(seed, table_name)
    rows = []
    table = TABLE_SCHEMAS[table_name]
    for i in range(num_rows):
        rows.append({c["name"]: _semantic_value(table_name, c, i, rng) for c in table["columns"]})
    return spark.createDataFrame(rows, schema=get_table_schema(table_name))


def _cast_and_reorder_csv_df(df: Any, table_name: str, validate_required: bool = True):
    from pyspark.sql import functions as F
    table = TABLE_SCHEMAS[table_name]
    expected = [c["name"] for c in table["columns"]]
    lower_to_actual = {col.lower(): col for col in df.columns}
    for c in table["columns"]:
        expected_name = c["name"]
        source_name = lower_to_actual.get(expected_name.lower())
        if source_name is None:
            if c.get("required") and validate_required:
                raise ValueError(f"CSV is missing required column {expected_name!r} for table {table_name!r}")
            df = df.withColumn(expected_name, F.lit(None).cast(_spark_type(c.get("logicalType", "string"))))
        elif source_name != expected_name:
            df = df.withColumnRenamed(source_name, expected_name)
        df = df.withColumn(expected_name, F.col(expected_name).cast(_spark_type(c.get("logicalType", "string"))))
    return df.select(*expected)


def _load_table_csv(spark: Any, table_name: str, csv_path: str, target_table: Optional[str] = None,
                    target_path: Optional[str] = None, mode: str = "append", header: bool = True,
                    delimiter: str = ",", output_format: str = "parquet",
                    validate_required: bool = True):
    reader = (spark.read.option("header", str(header).lower())
                    .option("delimiter", delimiter)
                    .option("multiLine", "true")
                    .option("escape", '"'))
    df = reader.csv(csv_path)
    df = _cast_and_reorder_csv_df(df, table_name, validate_required=validate_required)
    if target_table:
        df.write.mode(mode).saveAsTable(target_table)
    if target_path:
        df.write.mode(mode).format(output_format).save(target_path)
    return df



def generate_telco_customer_profile(spark: Any, num_rows: int, seed: int = 42):
    """
    Generate synthetic rows for ``telco_customer_profile`` as a PySpark DataFrame.

    Table definition:
        Core customer profile used by CRM, billing, support, marketing consent, and KYC workflows for Egyptian telecom operators.

    Domain and granularity:
        Domain is ``telecom_crm``; granularity is ``One row per natural person or business customer account holder.``.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``. Agents should pass the Spark
            session already available in the execution environment.
        num_rows:
            Number of synthetic rows to create. Use small values for scanner
            smoke tests and larger values for benchmark or performance tests.
        seed:
            Deterministic seed. Keeping the same seed and row count produces
            stable golden-test records.

    Output:
        ``pyspark.sql.DataFrame`` with 11 columns, including 9
        PII-classified columns. Representative PII semantic types include:
        CUSTOMER_ID, DATE_OF_BIRTH, EGYPTIAN_MOBILE, EMAIL_ADDRESS, GENDER_INDICATOR, NRP, PERSON, PHONE_NUMBER.

    Example:
        >>> df = generate_telco_customer_profile(spark, num_rows=100, seed=7)
        >>> df.write.mode("overwrite").saveAsTable("golden.telco_customer_profile")

    Agent-use hint:
        Use this function when a workflow asks to create a clean synthetic source
        table for PII scanner comparison. The returned DataFrame is already typed
        and column-ordered according to the ODCS-style catalogue.
    """
    return _generate_table(spark, "telco_customer_profile", num_rows=num_rows, seed=seed)


def load_telco_customer_profile_csv(spark: Any, csv_path: str, target_table: Optional[str] = None,
                   target_path: Optional[str] = None, mode: str = "append", header: bool = True,
                   delimiter: str = ",", output_format: str = "parquet",
                   validate_required: bool = True):
    """
    Load a CSV file into the ``telco_customer_profile`` schema and optionally append it.

    Table definition:
        Core customer profile used by CRM, billing, support, marketing consent, and KYC workflows for Egyptian telecom operators.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``.
        csv_path:
            Path to the source CSV file or folder. The CSV should contain the
            expected columns. Column matching is case-insensitive, and columns are
            reordered to match the target schema.
        target_table:
            Optional Spark table name, for example ``"golden.telco_customer_profile"``.
            When provided, records are written using ``saveAsTable``.
        target_path:
            Optional filesystem/object-store path. When provided, records are
            written using ``DataFrameWriter.format(output_format).save``.
        mode:
            Spark write mode such as ``"append"`` or ``"overwrite"``. The default
            is ``"append"`` because this function is designed to add records.
        header:
            Whether the CSV file has a header row. Default is ``True``.
        delimiter:
            CSV delimiter. Default is comma.
        output_format:
            Storage format for ``target_path`` writes. Default is ``"parquet"``;
            use ``"delta"`` only when Delta Lake is installed in the Spark runtime.
        validate_required:
            If ``True``, missing required columns raise ``ValueError``. If
            ``False``, missing columns are added as nulls and cast to the target
            schema.

    Required columns:
        customer_id, full_name_en, primary_mobile

    Output:
        ``pyspark.sql.DataFrame`` cast and ordered according to the target schema.
        If ``target_table`` or ``target_path`` is provided, the same DataFrame is
        also appended or overwritten according to ``mode``.

    Example:
        >>> df = load_telco_customer_profile_csv(
        ...     spark,
        ...     csv_path="/mnt/landing/telco_customer_profile.csv",
        ...     target_table="golden.telco_customer_profile",
        ...     mode="append"
        ... )

    Agent-use hint:
        Use this function when a user uploads CSV rows to enrich a golden table.
        If the agent is unsure whether the CSV has all required columns, call with
        ``validate_required=True`` first; on failure, ask the user for the missing
        columns or retry with ``validate_required=False`` only for exploratory
        profiling.
    """
    return _load_table_csv(
        spark=spark,
        table_name="telco_customer_profile",
        csv_path=csv_path,
        target_table=target_table,
        target_path=target_path,
        mode=mode,
        header=header,
        delimiter=delimiter,
        output_format=output_format,
        validate_required=validate_required,
    )


def generate_telco_kyc_identity_document(spark: Any, num_rows: int, seed: int = 42):
    """
    Generate synthetic rows for ``telco_kyc_identity_document`` as a PySpark DataFrame.

    Table definition:
        Stores identity-document attributes captured during SIM registration, account opening, or customer due diligence.

    Domain and granularity:
        Domain is ``telecom_kyc``; granularity is ``One row per identity document submitted by a customer.``.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``. Agents should pass the Spark
            session already available in the execution environment.
        num_rows:
            Number of synthetic rows to create. Use small values for scanner
            smoke tests and larger values for benchmark or performance tests.
        seed:
            Deterministic seed. Keeping the same seed and row count produces
            stable golden-test records.

    Output:
        ``pyspark.sql.DataFrame`` with 10 columns, including 8
        PII-classified columns. Representative PII semantic types include:
        BIOMETRIC_DATA, CUSTOMER_ID, EG_DRIVER_LICENSE, EG_MILITARY_ID, EG_NATIONAL_ID, EG_PASSPORT, EG_RESIDENCY_PERMIT, EG_SYNDICATE_ID.

    Example:
        >>> df = generate_telco_kyc_identity_document(spark, num_rows=100, seed=7)
        >>> df.write.mode("overwrite").saveAsTable("golden.telco_kyc_identity_document")

    Agent-use hint:
        Use this function when a workflow asks to create a clean synthetic source
        table for PII scanner comparison. The returned DataFrame is already typed
        and column-ordered according to the ODCS-style catalogue.
    """
    return _generate_table(spark, "telco_kyc_identity_document", num_rows=num_rows, seed=seed)


def load_telco_kyc_identity_document_csv(spark: Any, csv_path: str, target_table: Optional[str] = None,
                   target_path: Optional[str] = None, mode: str = "append", header: bool = True,
                   delimiter: str = ",", output_format: str = "parquet",
                   validate_required: bool = True):
    """
    Load a CSV file into the ``telco_kyc_identity_document`` schema and optionally append it.

    Table definition:
        Stores identity-document attributes captured during SIM registration, account opening, or customer due diligence.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``.
        csv_path:
            Path to the source CSV file or folder. The CSV should contain the
            expected columns. Column matching is case-insensitive, and columns are
            reordered to match the target schema.
        target_table:
            Optional Spark table name, for example ``"golden.telco_kyc_identity_document"``.
            When provided, records are written using ``saveAsTable``.
        target_path:
            Optional filesystem/object-store path. When provided, records are
            written using ``DataFrameWriter.format(output_format).save``.
        mode:
            Spark write mode such as ``"append"`` or ``"overwrite"``. The default
            is ``"append"`` because this function is designed to add records.
        header:
            Whether the CSV file has a header row. Default is ``True``.
        delimiter:
            CSV delimiter. Default is comma.
        output_format:
            Storage format for ``target_path`` writes. Default is ``"parquet"``;
            use ``"delta"`` only when Delta Lake is installed in the Spark runtime.
        validate_required:
            If ``True``, missing required columns raise ``ValueError``. If
            ``False``, missing columns are added as nulls and cast to the target
            schema.

    Required columns:
        kyc_document_id, customer_id

    Output:
        ``pyspark.sql.DataFrame`` cast and ordered according to the target schema.
        If ``target_table`` or ``target_path`` is provided, the same DataFrame is
        also appended or overwritten according to ``mode``.

    Example:
        >>> df = load_telco_kyc_identity_document_csv(
        ...     spark,
        ...     csv_path="/mnt/landing/telco_kyc_identity_document.csv",
        ...     target_table="golden.telco_kyc_identity_document",
        ...     mode="append"
        ... )

    Agent-use hint:
        Use this function when a user uploads CSV rows to enrich a golden table.
        If the agent is unsure whether the CSV has all required columns, call with
        ``validate_required=True`` first; on failure, ask the user for the missing
        columns or retry with ``validate_required=False`` only for exploratory
        profiling.
    """
    return _load_table_csv(
        spark=spark,
        table_name="telco_kyc_identity_document",
        csv_path=csv_path,
        target_table=target_table,
        target_path=target_path,
        mode=mode,
        header=header,
        delimiter=delimiter,
        output_format=output_format,
        validate_required=validate_required,
    )


def generate_telco_subscriber_sim_registry(spark: Any, num_rows: int, seed: int = 42):
    """
    Generate synthetic rows for ``telco_subscriber_sim_registry`` as a PySpark DataFrame.

    Table definition:
        Represents active and historical SIM subscriptions, including Egyptian MSISDN, IMSI, ICCID, and operator-specific identifiers.

    Domain and granularity:
        Domain is ``telecom_bss``; granularity is ``One row per SIM subscription lifecycle instance.``.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``. Agents should pass the Spark
            session already available in the execution environment.
        num_rows:
            Number of synthetic rows to create. Use small values for scanner
            smoke tests and larger values for benchmark or performance tests.
        seed:
            Deterministic seed. Keeping the same seed and row count produces
            stable golden-test records.

    Output:
        ``pyspark.sql.DataFrame`` with 10 columns, including 7
        PII-classified columns. Representative PII semantic types include:
        CUSTOMER_ID, ICCID, IMSI, MSISDN, PIN_CODE_SIM, PUK_CODE, SUBSCRIBER_ID.

    Example:
        >>> df = generate_telco_subscriber_sim_registry(spark, num_rows=100, seed=7)
        >>> df.write.mode("overwrite").saveAsTable("golden.telco_subscriber_sim_registry")

    Agent-use hint:
        Use this function when a workflow asks to create a clean synthetic source
        table for PII scanner comparison. The returned DataFrame is already typed
        and column-ordered according to the ODCS-style catalogue.
    """
    return _generate_table(spark, "telco_subscriber_sim_registry", num_rows=num_rows, seed=seed)


def load_telco_subscriber_sim_registry_csv(spark: Any, csv_path: str, target_table: Optional[str] = None,
                   target_path: Optional[str] = None, mode: str = "append", header: bool = True,
                   delimiter: str = ",", output_format: str = "parquet",
                   validate_required: bool = True):
    """
    Load a CSV file into the ``telco_subscriber_sim_registry`` schema and optionally append it.

    Table definition:
        Represents active and historical SIM subscriptions, including Egyptian MSISDN, IMSI, ICCID, and operator-specific identifiers.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``.
        csv_path:
            Path to the source CSV file or folder. The CSV should contain the
            expected columns. Column matching is case-insensitive, and columns are
            reordered to match the target schema.
        target_table:
            Optional Spark table name, for example ``"golden.telco_subscriber_sim_registry"``.
            When provided, records are written using ``saveAsTable``.
        target_path:
            Optional filesystem/object-store path. When provided, records are
            written using ``DataFrameWriter.format(output_format).save``.
        mode:
            Spark write mode such as ``"append"`` or ``"overwrite"``. The default
            is ``"append"`` because this function is designed to add records.
        header:
            Whether the CSV file has a header row. Default is ``True``.
        delimiter:
            CSV delimiter. Default is comma.
        output_format:
            Storage format for ``target_path`` writes. Default is ``"parquet"``;
            use ``"delta"`` only when Delta Lake is installed in the Spark runtime.
        validate_required:
            If ``True``, missing required columns raise ``ValueError``. If
            ``False``, missing columns are added as nulls and cast to the target
            schema.

    Required columns:
        subscription_id, customer_id, msisdn, imsi, iccid

    Output:
        ``pyspark.sql.DataFrame`` cast and ordered according to the target schema.
        If ``target_table`` or ``target_path`` is provided, the same DataFrame is
        also appended or overwritten according to ``mode``.

    Example:
        >>> df = load_telco_subscriber_sim_registry_csv(
        ...     spark,
        ...     csv_path="/mnt/landing/telco_subscriber_sim_registry.csv",
        ...     target_table="golden.telco_subscriber_sim_registry",
        ...     mode="append"
        ... )

    Agent-use hint:
        Use this function when a user uploads CSV rows to enrich a golden table.
        If the agent is unsure whether the CSV has all required columns, call with
        ``validate_required=True`` first; on failure, ask the user for the missing
        columns or retry with ``validate_required=False`` only for exploratory
        profiling.
    """
    return _load_table_csv(
        spark=spark,
        table_name="telco_subscriber_sim_registry",
        csv_path=csv_path,
        target_table=target_table,
        target_path=target_path,
        mode=mode,
        header=header,
        delimiter=delimiter,
        output_format=output_format,
        validate_required=validate_required,
    )


def generate_telco_device_and_cpe_inventory(spark: Any, num_rows: int, seed: int = 42):
    """
    Generate synthetic rows for ``telco_device_and_cpe_inventory`` as a PySpark DataFrame.

    Table definition:
        Tracks handset, router, ONT, and eSIM/device identifiers associated with subscribers and fixed broadband customers.

    Domain and granularity:
        Domain is ``telecom_oss``; granularity is ``One row per customer-associated device or CPE asset.``.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``. Agents should pass the Spark
            session already available in the execution environment.
        num_rows:
            Number of synthetic rows to create. Use small values for scanner
            smoke tests and larger values for benchmark or performance tests.
        seed:
            Deterministic seed. Keeping the same seed and row count produces
            stable golden-test records.

    Output:
        ``pyspark.sql.DataFrame`` with 10 columns, including 9
        PII-classified columns. Representative PII semantic types include:
        CUSTOMER_ID, EMBEDDED_SIM_EID, IMEI, IMEISV, LOCATION_ADDRESS, MAC_ADDRESS, ONT_SERIAL, ROUTER_SERIAL_ALPHANUM, SUBSCRIBER_ID.

    Example:
        >>> df = generate_telco_device_and_cpe_inventory(spark, num_rows=100, seed=7)
        >>> df.write.mode("overwrite").saveAsTable("golden.telco_device_and_cpe_inventory")

    Agent-use hint:
        Use this function when a workflow asks to create a clean synthetic source
        table for PII scanner comparison. The returned DataFrame is already typed
        and column-ordered according to the ODCS-style catalogue.
    """
    return _generate_table(spark, "telco_device_and_cpe_inventory", num_rows=num_rows, seed=seed)


def load_telco_device_and_cpe_inventory_csv(spark: Any, csv_path: str, target_table: Optional[str] = None,
                   target_path: Optional[str] = None, mode: str = "append", header: bool = True,
                   delimiter: str = ",", output_format: str = "parquet",
                   validate_required: bool = True):
    """
    Load a CSV file into the ``telco_device_and_cpe_inventory`` schema and optionally append it.

    Table definition:
        Tracks handset, router, ONT, and eSIM/device identifiers associated with subscribers and fixed broadband customers.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``.
        csv_path:
            Path to the source CSV file or folder. The CSV should contain the
            expected columns. Column matching is case-insensitive, and columns are
            reordered to match the target schema.
        target_table:
            Optional Spark table name, for example ``"golden.telco_device_and_cpe_inventory"``.
            When provided, records are written using ``saveAsTable``.
        target_path:
            Optional filesystem/object-store path. When provided, records are
            written using ``DataFrameWriter.format(output_format).save``.
        mode:
            Spark write mode such as ``"append"`` or ``"overwrite"``. The default
            is ``"append"`` because this function is designed to add records.
        header:
            Whether the CSV file has a header row. Default is ``True``.
        delimiter:
            CSV delimiter. Default is comma.
        output_format:
            Storage format for ``target_path`` writes. Default is ``"parquet"``;
            use ``"delta"`` only when Delta Lake is installed in the Spark runtime.
        validate_required:
            If ``True``, missing required columns raise ``ValueError``. If
            ``False``, missing columns are added as nulls and cast to the target
            schema.

    Required columns:
        device_asset_id

    Output:
        ``pyspark.sql.DataFrame`` cast and ordered according to the target schema.
        If ``target_table`` or ``target_path`` is provided, the same DataFrame is
        also appended or overwritten according to ``mode``.

    Example:
        >>> df = load_telco_device_and_cpe_inventory_csv(
        ...     spark,
        ...     csv_path="/mnt/landing/telco_device_and_cpe_inventory.csv",
        ...     target_table="golden.telco_device_and_cpe_inventory",
        ...     mode="append"
        ... )

    Agent-use hint:
        Use this function when a user uploads CSV rows to enrich a golden table.
        If the agent is unsure whether the CSV has all required columns, call with
        ``validate_required=True`` first; on failure, ask the user for the missing
        columns or retry with ``validate_required=False`` only for exploratory
        profiling.
    """
    return _load_table_csv(
        spark=spark,
        table_name="telco_device_and_cpe_inventory",
        csv_path=csv_path,
        target_table=target_table,
        target_path=target_path,
        mode=mode,
        header=header,
        delimiter=delimiter,
        output_format=output_format,
        validate_required=validate_required,
    )


def generate_telco_cdr_event(spark: Any, num_rows: int, seed: int = 42):
    """
    Generate synthetic rows for ``telco_cdr_event`` as a PySpark DataFrame.

    Table definition:
        Synthetic call/SMS/data event table for testing PII detection in CDR-style transactional datasets.

    Domain and granularity:
        Domain is ``telecom_network``; granularity is ``One row per call, SMS, or data session event.``.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``. Agents should pass the Spark
            session already available in the execution environment.
        num_rows:
            Number of synthetic rows to create. Use small values for scanner
            smoke tests and larger values for benchmark or performance tests.
        seed:
            Deterministic seed. Keeping the same seed and row count produces
            stable golden-test records.

    Output:
        ``pyspark.sql.DataFrame`` with 13 columns, including 12
        PII-classified columns. Representative PII semantic types include:
        CDR_RECORD_ID, CELL_GLOBAL_IDENTITY, GPS_LATITUDE, GPS_LONGITUDE, IMEI, IMSI, IPV4_CGNAT, IP_ADDRESS, LAC, MSISDN, TAC_LTE.

    Example:
        >>> df = generate_telco_cdr_event(spark, num_rows=100, seed=7)
        >>> df.write.mode("overwrite").saveAsTable("golden.telco_cdr_event")

    Agent-use hint:
        Use this function when a workflow asks to create a clean synthetic source
        table for PII scanner comparison. The returned DataFrame is already typed
        and column-ordered according to the ODCS-style catalogue.
    """
    return _generate_table(spark, "telco_cdr_event", num_rows=num_rows, seed=seed)


def load_telco_cdr_event_csv(spark: Any, csv_path: str, target_table: Optional[str] = None,
                   target_path: Optional[str] = None, mode: str = "append", header: bool = True,
                   delimiter: str = ",", output_format: str = "parquet",
                   validate_required: bool = True):
    """
    Load a CSV file into the ``telco_cdr_event`` schema and optionally append it.

    Table definition:
        Synthetic call/SMS/data event table for testing PII detection in CDR-style transactional datasets.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``.
        csv_path:
            Path to the source CSV file or folder. The CSV should contain the
            expected columns. Column matching is case-insensitive, and columns are
            reordered to match the target schema.
        target_table:
            Optional Spark table name, for example ``"golden.telco_cdr_event"``.
            When provided, records are written using ``saveAsTable``.
        target_path:
            Optional filesystem/object-store path. When provided, records are
            written using ``DataFrameWriter.format(output_format).save``.
        mode:
            Spark write mode such as ``"append"`` or ``"overwrite"``. The default
            is ``"append"`` because this function is designed to add records.
        header:
            Whether the CSV file has a header row. Default is ``True``.
        delimiter:
            CSV delimiter. Default is comma.
        output_format:
            Storage format for ``target_path`` writes. Default is ``"parquet"``;
            use ``"delta"`` only when Delta Lake is installed in the Spark runtime.
        validate_required:
            If ``True``, missing required columns raise ``ValueError``. If
            ``False``, missing columns are added as nulls and cast to the target
            schema.

    Required columns:
        cdr_record_id, event_timestamp, a_party_msisdn

    Output:
        ``pyspark.sql.DataFrame`` cast and ordered according to the target schema.
        If ``target_table`` or ``target_path`` is provided, the same DataFrame is
        also appended or overwritten according to ``mode``.

    Example:
        >>> df = load_telco_cdr_event_csv(
        ...     spark,
        ...     csv_path="/mnt/landing/telco_cdr_event.csv",
        ...     target_table="golden.telco_cdr_event",
        ...     mode="append"
        ... )

    Agent-use hint:
        Use this function when a user uploads CSV rows to enrich a golden table.
        If the agent is unsure whether the CSV has all required columns, call with
        ``validate_required=True`` first; on failure, ask the user for the missing
        columns or retry with ``validate_required=False`` only for exploratory
        profiling.
    """
    return _load_table_csv(
        spark=spark,
        table_name="telco_cdr_event",
        csv_path=csv_path,
        target_table=target_table,
        target_path=target_path,
        mode=mode,
        header=header,
        delimiter=delimiter,
        output_format=output_format,
        validate_required=validate_required,
    )


def generate_telco_billing_invoice(spark: Any, num_rows: int, seed: int = 42):
    """
    Generate synthetic rows for ``telco_billing_invoice`` as a PySpark DataFrame.

    Table definition:
        Billing-account and invoice data for postpaid, prepaid hybrid, and fixed-line telecom services.

    Domain and granularity:
        Domain is ``telecom_billing``; granularity is ``One row per issued invoice or billing cycle document.``.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``. Agents should pass the Spark
            session already available in the execution environment.
        num_rows:
            Number of synthetic rows to create. Use small values for scanner
            smoke tests and larger values for benchmark or performance tests.
        seed:
            Deterministic seed. Keeping the same seed and row count produces
            stable golden-test records.

    Output:
        ``pyspark.sql.DataFrame`` with 11 columns, including 10
        PII-classified columns. Representative PII semantic types include:
        BILLING_ACCOUNT_NUMBER, CREDIT_CARD, CUSTOMER_ID, EGYPTIAN_MOBILE, EG_COMMERCIAL_REGISTRY_NUMBER, EG_TAX_REGISTRATION_NUMBER, IBAN_CODE, INVOICE_NUMBER, LOCATION_ADDRESS, PERSON.

    Example:
        >>> df = generate_telco_billing_invoice(spark, num_rows=100, seed=7)
        >>> df.write.mode("overwrite").saveAsTable("golden.telco_billing_invoice")

    Agent-use hint:
        Use this function when a workflow asks to create a clean synthetic source
        table for PII scanner comparison. The returned DataFrame is already typed
        and column-ordered according to the ODCS-style catalogue.
    """
    return _generate_table(spark, "telco_billing_invoice", num_rows=num_rows, seed=seed)


def load_telco_billing_invoice_csv(spark: Any, csv_path: str, target_table: Optional[str] = None,
                   target_path: Optional[str] = None, mode: str = "append", header: bool = True,
                   delimiter: str = ",", output_format: str = "parquet",
                   validate_required: bool = True):
    """
    Load a CSV file into the ``telco_billing_invoice`` schema and optionally append it.

    Table definition:
        Billing-account and invoice data for postpaid, prepaid hybrid, and fixed-line telecom services.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``.
        csv_path:
            Path to the source CSV file or folder. The CSV should contain the
            expected columns. Column matching is case-insensitive, and columns are
            reordered to match the target schema.
        target_table:
            Optional Spark table name, for example ``"golden.telco_billing_invoice"``.
            When provided, records are written using ``saveAsTable``.
        target_path:
            Optional filesystem/object-store path. When provided, records are
            written using ``DataFrameWriter.format(output_format).save``.
        mode:
            Spark write mode such as ``"append"`` or ``"overwrite"``. The default
            is ``"append"`` because this function is designed to add records.
        header:
            Whether the CSV file has a header row. Default is ``True``.
        delimiter:
            CSV delimiter. Default is comma.
        output_format:
            Storage format for ``target_path`` writes. Default is ``"parquet"``;
            use ``"delta"`` only when Delta Lake is installed in the Spark runtime.
        validate_required:
            If ``True``, missing required columns raise ``ValueError``. If
            ``False``, missing columns are added as nulls and cast to the target
            schema.

    Required columns:
        invoice_id, billing_account_number

    Output:
        ``pyspark.sql.DataFrame`` cast and ordered according to the target schema.
        If ``target_table`` or ``target_path`` is provided, the same DataFrame is
        also appended or overwritten according to ``mode``.

    Example:
        >>> df = load_telco_billing_invoice_csv(
        ...     spark,
        ...     csv_path="/mnt/landing/telco_billing_invoice.csv",
        ...     target_table="golden.telco_billing_invoice",
        ...     mode="append"
        ... )

    Agent-use hint:
        Use this function when a user uploads CSV rows to enrich a golden table.
        If the agent is unsure whether the CSV has all required columns, call with
        ``validate_required=True`` first; on failure, ask the user for the missing
        columns or retry with ``validate_required=False`` only for exploratory
        profiling.
    """
    return _load_table_csv(
        spark=spark,
        table_name="telco_billing_invoice",
        csv_path=csv_path,
        target_table=target_table,
        target_path=target_path,
        mode=mode,
        header=header,
        delimiter=delimiter,
        output_format=output_format,
        validate_required=validate_required,
    )


def generate_eshop_customer_account(spark: Any, num_rows: int, seed: int = 42):
    """
    Generate synthetic rows for ``eshop_customer_account`` as a PySpark DataFrame.

    Table definition:
        Customer account table for Egyptian e-commerce, marketplace, and digital retail use cases.

    Domain and granularity:
        Domain is ``ecommerce_crm``; granularity is ``One row per registered customer account.``.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``. Agents should pass the Spark
            session already available in the execution environment.
        num_rows:
            Number of synthetic rows to create. Use small values for scanner
            smoke tests and larger values for benchmark or performance tests.
        seed:
            Deterministic seed. Keeping the same seed and row count produces
            stable golden-test records.

    Output:
        ``pyspark.sql.DataFrame`` with 9 columns, including 8
        PII-classified columns. Representative PII semantic types include:
        CSRF_TOKEN, CUSTOMER_ID, EGYPTIAN_MOBILE, EMAIL_ADDRESS, FACEBOOK_PROFILE_URL, HASH_BCRYPT, PERSON, SESSION_ID_ALPHANUM.

    Example:
        >>> df = generate_eshop_customer_account(spark, num_rows=100, seed=7)
        >>> df.write.mode("overwrite").saveAsTable("golden.eshop_customer_account")

    Agent-use hint:
        Use this function when a workflow asks to create a clean synthetic source
        table for PII scanner comparison. The returned DataFrame is already typed
        and column-ordered according to the ODCS-style catalogue.
    """
    return _generate_table(spark, "eshop_customer_account", num_rows=num_rows, seed=seed)


def load_eshop_customer_account_csv(spark: Any, csv_path: str, target_table: Optional[str] = None,
                   target_path: Optional[str] = None, mode: str = "append", header: bool = True,
                   delimiter: str = ",", output_format: str = "parquet",
                   validate_required: bool = True):
    """
    Load a CSV file into the ``eshop_customer_account`` schema and optionally append it.

    Table definition:
        Customer account table for Egyptian e-commerce, marketplace, and digital retail use cases.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``.
        csv_path:
            Path to the source CSV file or folder. The CSV should contain the
            expected columns. Column matching is case-insensitive, and columns are
            reordered to match the target schema.
        target_table:
            Optional Spark table name, for example ``"golden.eshop_customer_account"``.
            When provided, records are written using ``saveAsTable``.
        target_path:
            Optional filesystem/object-store path. When provided, records are
            written using ``DataFrameWriter.format(output_format).save``.
        mode:
            Spark write mode such as ``"append"`` or ``"overwrite"``. The default
            is ``"append"`` because this function is designed to add records.
        header:
            Whether the CSV file has a header row. Default is ``True``.
        delimiter:
            CSV delimiter. Default is comma.
        output_format:
            Storage format for ``target_path`` writes. Default is ``"parquet"``;
            use ``"delta"`` only when Delta Lake is installed in the Spark runtime.
        validate_required:
            If ``True``, missing required columns raise ``ValueError``. If
            ``False``, missing columns are added as nulls and cast to the target
            schema.

    Required columns:
        eshop_customer_id, full_name, email_address, mobile_number

    Output:
        ``pyspark.sql.DataFrame`` cast and ordered according to the target schema.
        If ``target_table`` or ``target_path`` is provided, the same DataFrame is
        also appended or overwritten according to ``mode``.

    Example:
        >>> df = load_eshop_customer_account_csv(
        ...     spark,
        ...     csv_path="/mnt/landing/eshop_customer_account.csv",
        ...     target_table="golden.eshop_customer_account",
        ...     mode="append"
        ... )

    Agent-use hint:
        Use this function when a user uploads CSV rows to enrich a golden table.
        If the agent is unsure whether the CSV has all required columns, call with
        ``validate_required=True`` first; on failure, ask the user for the missing
        columns or retry with ``validate_required=False`` only for exploratory
        profiling.
    """
    return _load_table_csv(
        spark=spark,
        table_name="eshop_customer_account",
        csv_path=csv_path,
        target_table=target_table,
        target_path=target_path,
        mode=mode,
        header=header,
        delimiter=delimiter,
        output_format=output_format,
        validate_required=validate_required,
    )


def generate_eshop_shipping_address(spark: Any, num_rows: int, seed: int = 42):
    """
    Generate synthetic rows for ``eshop_shipping_address`` as a PySpark DataFrame.

    Table definition:
        Address-book table containing Egyptian delivery-address components needed for e-commerce fulfillment.

    Domain and granularity:
        Domain is ``ecommerce_fulfillment``; granularity is ``One row per saved customer shipping address.``.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``. Agents should pass the Spark
            session already available in the execution environment.
        num_rows:
            Number of synthetic rows to create. Use small values for scanner
            smoke tests and larger values for benchmark or performance tests.
        seed:
            Deterministic seed. Keeping the same seed and row count produces
            stable golden-test records.

    Output:
        ``pyspark.sql.DataFrame`` with 12 columns, including 11
        PII-classified columns. Representative PII semantic types include:
        CUSTOMER_ID, EGYPTIAN_MOBILE, EG_BUILDING_DETAILS, EG_DISTRICT, EG_POSTAL_CODE, GOVERNORATE, GPS_PAIR, LOCATION_ADDRESS, PERSON, WHAT3WORDS.

    Example:
        >>> df = generate_eshop_shipping_address(spark, num_rows=100, seed=7)
        >>> df.write.mode("overwrite").saveAsTable("golden.eshop_shipping_address")

    Agent-use hint:
        Use this function when a workflow asks to create a clean synthetic source
        table for PII scanner comparison. The returned DataFrame is already typed
        and column-ordered according to the ODCS-style catalogue.
    """
    return _generate_table(spark, "eshop_shipping_address", num_rows=num_rows, seed=seed)


def load_eshop_shipping_address_csv(spark: Any, csv_path: str, target_table: Optional[str] = None,
                   target_path: Optional[str] = None, mode: str = "append", header: bool = True,
                   delimiter: str = ",", output_format: str = "parquet",
                   validate_required: bool = True):
    """
    Load a CSV file into the ``eshop_shipping_address`` schema and optionally append it.

    Table definition:
        Address-book table containing Egyptian delivery-address components needed for e-commerce fulfillment.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``.
        csv_path:
            Path to the source CSV file or folder. The CSV should contain the
            expected columns. Column matching is case-insensitive, and columns are
            reordered to match the target schema.
        target_table:
            Optional Spark table name, for example ``"golden.eshop_shipping_address"``.
            When provided, records are written using ``saveAsTable``.
        target_path:
            Optional filesystem/object-store path. When provided, records are
            written using ``DataFrameWriter.format(output_format).save``.
        mode:
            Spark write mode such as ``"append"`` or ``"overwrite"``. The default
            is ``"append"`` because this function is designed to add records.
        header:
            Whether the CSV file has a header row. Default is ``True``.
        delimiter:
            CSV delimiter. Default is comma.
        output_format:
            Storage format for ``target_path`` writes. Default is ``"parquet"``;
            use ``"delta"`` only when Delta Lake is installed in the Spark runtime.
        validate_required:
            If ``True``, missing required columns raise ``ValueError``. If
            ``False``, missing columns are added as nulls and cast to the target
            schema.

    Required columns:
        address_id, recipient_name, recipient_mobile

    Output:
        ``pyspark.sql.DataFrame`` cast and ordered according to the target schema.
        If ``target_table`` or ``target_path`` is provided, the same DataFrame is
        also appended or overwritten according to ``mode``.

    Example:
        >>> df = load_eshop_shipping_address_csv(
        ...     spark,
        ...     csv_path="/mnt/landing/eshop_shipping_address.csv",
        ...     target_table="golden.eshop_shipping_address",
        ...     mode="append"
        ... )

    Agent-use hint:
        Use this function when a user uploads CSV rows to enrich a golden table.
        If the agent is unsure whether the CSV has all required columns, call with
        ``validate_required=True`` first; on failure, ask the user for the missing
        columns or retry with ``validate_required=False`` only for exploratory
        profiling.
    """
    return _load_table_csv(
        spark=spark,
        table_name="eshop_shipping_address",
        csv_path=csv_path,
        target_table=target_table,
        target_path=target_path,
        mode=mode,
        header=header,
        delimiter=delimiter,
        output_format=output_format,
        validate_required=validate_required,
    )


def generate_eshop_order_header(spark: Any, num_rows: int, seed: int = 42):
    """
    Generate synthetic rows for ``eshop_order_header`` as a PySpark DataFrame.

    Table definition:
        Order-level table connecting customers, delivery address, payment channel, and fulfillment status.

    Domain and granularity:
        Domain is ``ecommerce_orders``; granularity is ``One row per placed order.``.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``. Agents should pass the Spark
            session already available in the execution environment.
        num_rows:
            Number of synthetic rows to create. Use small values for scanner
            smoke tests and larger values for benchmark or performance tests.
        seed:
            Deterministic seed. Keeping the same seed and row count produces
            stable golden-test records.

    Output:
        ``pyspark.sql.DataFrame`` with 9 columns, including 5
        PII-classified columns. Representative PII semantic types include:
        CUSTOMER_ID, EGYPTIAN_MOBILE, LOCATION_ADDRESS, PERSON, SHIPPING_TRACKING_NUMBER.

    Example:
        >>> df = generate_eshop_order_header(spark, num_rows=100, seed=7)
        >>> df.write.mode("overwrite").saveAsTable("golden.eshop_order_header")

    Agent-use hint:
        Use this function when a workflow asks to create a clean synthetic source
        table for PII scanner comparison. The returned DataFrame is already typed
        and column-ordered according to the ODCS-style catalogue.
    """
    return _generate_table(spark, "eshop_order_header", num_rows=num_rows, seed=seed)


def load_eshop_order_header_csv(spark: Any, csv_path: str, target_table: Optional[str] = None,
                   target_path: Optional[str] = None, mode: str = "append", header: bool = True,
                   delimiter: str = ",", output_format: str = "parquet",
                   validate_required: bool = True):
    """
    Load a CSV file into the ``eshop_order_header`` schema and optionally append it.

    Table definition:
        Order-level table connecting customers, delivery address, payment channel, and fulfillment status.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``.
        csv_path:
            Path to the source CSV file or folder. The CSV should contain the
            expected columns. Column matching is case-insensitive, and columns are
            reordered to match the target schema.
        target_table:
            Optional Spark table name, for example ``"golden.eshop_order_header"``.
            When provided, records are written using ``saveAsTable``.
        target_path:
            Optional filesystem/object-store path. When provided, records are
            written using ``DataFrameWriter.format(output_format).save``.
        mode:
            Spark write mode such as ``"append"`` or ``"overwrite"``. The default
            is ``"append"`` because this function is designed to add records.
        header:
            Whether the CSV file has a header row. Default is ``True``.
        delimiter:
            CSV delimiter. Default is comma.
        output_format:
            Storage format for ``target_path`` writes. Default is ``"parquet"``;
            use ``"delta"`` only when Delta Lake is installed in the Spark runtime.
        validate_required:
            If ``True``, missing required columns raise ``ValueError``. If
            ``False``, missing columns are added as nulls and cast to the target
            schema.

    Required columns:
        order_id, eshop_customer_id

    Output:
        ``pyspark.sql.DataFrame`` cast and ordered according to the target schema.
        If ``target_table`` or ``target_path`` is provided, the same DataFrame is
        also appended or overwritten according to ``mode``.

    Example:
        >>> df = load_eshop_order_header_csv(
        ...     spark,
        ...     csv_path="/mnt/landing/eshop_order_header.csv",
        ...     target_table="golden.eshop_order_header",
        ...     mode="append"
        ... )

    Agent-use hint:
        Use this function when a user uploads CSV rows to enrich a golden table.
        If the agent is unsure whether the CSV has all required columns, call with
        ``validate_required=True`` first; on failure, ask the user for the missing
        columns or retry with ``validate_required=False`` only for exploratory
        profiling.
    """
    return _load_table_csv(
        spark=spark,
        table_name="eshop_order_header",
        csv_path=csv_path,
        target_table=target_table,
        target_path=target_path,
        mode=mode,
        header=header,
        delimiter=delimiter,
        output_format=output_format,
        validate_required=validate_required,
    )


def generate_fintech_payment_transaction(spark: Any, num_rows: int, seed: int = 42):
    """
    Generate synthetic rows for ``fintech_payment_transaction`` as a PySpark DataFrame.

    Table definition:
        Unified payment-transaction table covering card, Meeza, Fawry, mobile-wallet, InstaPay, and bank-transfer flows used by telecom and e-commerce.

    Domain and granularity:
        Domain is ``fintech_payments``; granularity is ``One row per payment authorization, capture, transfer, or settlement transaction.``.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``. Agents should pass the Spark
            session already available in the execution environment.
        num_rows:
            Number of synthetic rows to create. Use small values for scanner
            smoke tests and larger values for benchmark or performance tests.
        seed:
            Deterministic seed. Keeping the same seed and row count produces
            stable golden-test records.

    Output:
        ``pyspark.sql.DataFrame`` with 14 columns, including 13
        PII-classified columns. Representative PII semantic types include:
        CARD_EXPIRY, CREDIT_CARD, CUSTOMER_ID, CVV, EG_BANK_ACCOUNT, EG_FAWRY_REFERENCE, EG_MEEZA_CARD, EG_MOBILE_WALLET_NUMBER, IBAN_CODE, INSTAPAY_ADDRESS, PERSON, STRIPE_PAYMENT_INTENT.

    Example:
        >>> df = generate_fintech_payment_transaction(spark, num_rows=100, seed=7)
        >>> df.write.mode("overwrite").saveAsTable("golden.fintech_payment_transaction")

    Agent-use hint:
        Use this function when a workflow asks to create a clean synthetic source
        table for PII scanner comparison. The returned DataFrame is already typed
        and column-ordered according to the ODCS-style catalogue.
    """
    return _generate_table(spark, "fintech_payment_transaction", num_rows=num_rows, seed=seed)


def load_fintech_payment_transaction_csv(spark: Any, csv_path: str, target_table: Optional[str] = None,
                   target_path: Optional[str] = None, mode: str = "append", header: bool = True,
                   delimiter: str = ",", output_format: str = "parquet",
                   validate_required: bool = True):
    """
    Load a CSV file into the ``fintech_payment_transaction`` schema and optionally append it.

    Table definition:
        Unified payment-transaction table covering card, Meeza, Fawry, mobile-wallet, InstaPay, and bank-transfer flows used by telecom and e-commerce.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``.
        csv_path:
            Path to the source CSV file or folder. The CSV should contain the
            expected columns. Column matching is case-insensitive, and columns are
            reordered to match the target schema.
        target_table:
            Optional Spark table name, for example ``"golden.fintech_payment_transaction"``.
            When provided, records are written using ``saveAsTable``.
        target_path:
            Optional filesystem/object-store path. When provided, records are
            written using ``DataFrameWriter.format(output_format).save``.
        mode:
            Spark write mode such as ``"append"`` or ``"overwrite"``. The default
            is ``"append"`` because this function is designed to add records.
        header:
            Whether the CSV file has a header row. Default is ``True``.
        delimiter:
            CSV delimiter. Default is comma.
        output_format:
            Storage format for ``target_path`` writes. Default is ``"parquet"``;
            use ``"delta"`` only when Delta Lake is installed in the Spark runtime.
        validate_required:
            If ``True``, missing required columns raise ``ValueError``. If
            ``False``, missing columns are added as nulls and cast to the target
            schema.

    Required columns:
        payment_transaction_id

    Output:
        ``pyspark.sql.DataFrame`` cast and ordered according to the target schema.
        If ``target_table`` or ``target_path`` is provided, the same DataFrame is
        also appended or overwritten according to ``mode``.

    Example:
        >>> df = load_fintech_payment_transaction_csv(
        ...     spark,
        ...     csv_path="/mnt/landing/fintech_payment_transaction.csv",
        ...     target_table="golden.fintech_payment_transaction",
        ...     mode="append"
        ... )

    Agent-use hint:
        Use this function when a user uploads CSV rows to enrich a golden table.
        If the agent is unsure whether the CSV has all required columns, call with
        ``validate_required=True`` first; on failure, ask the user for the missing
        columns or retry with ``validate_required=False`` only for exploratory
        profiling.
    """
    return _load_table_csv(
        spark=spark,
        table_name="fintech_payment_transaction",
        csv_path=csv_path,
        target_table=target_table,
        target_path=target_path,
        mode=mode,
        header=header,
        delimiter=delimiter,
        output_format=output_format,
        validate_required=validate_required,
    )


def generate_fintech_mobile_wallet_account(spark: Any, num_rows: int, seed: int = 42):
    """
    Generate synthetic rows for ``fintech_mobile_wallet_account`` as a PySpark DataFrame.

    Table definition:
        Mobile wallet account table for Vodafone Cash, Orange Cash, Etisalat Cash, WE Pay, and similar Egyptian wallet products.

    Domain and granularity:
        Domain is ``fintech_wallets``; granularity is ``One row per mobile wallet account.``.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``. Agents should pass the Spark
            session already available in the execution environment.
        num_rows:
            Number of synthetic rows to create. Use small values for scanner
            smoke tests and larger values for benchmark or performance tests.
        seed:
            Deterministic seed. Keeping the same seed and row count produces
            stable golden-test records.

    Output:
        ``pyspark.sql.DataFrame`` with 10 columns, including 8
        PII-classified columns. Representative PII semantic types include:
        CUSTOMER_ID, EG_MOBILE_WALLET_NUMBER, EG_NATIONAL_ID, HASH_BCRYPT, IMEI, IP_ADDRESS, PERSON.

    Example:
        >>> df = generate_fintech_mobile_wallet_account(spark, num_rows=100, seed=7)
        >>> df.write.mode("overwrite").saveAsTable("golden.fintech_mobile_wallet_account")

    Agent-use hint:
        Use this function when a workflow asks to create a clean synthetic source
        table for PII scanner comparison. The returned DataFrame is already typed
        and column-ordered according to the ODCS-style catalogue.
    """
    return _generate_table(spark, "fintech_mobile_wallet_account", num_rows=num_rows, seed=seed)


def load_fintech_mobile_wallet_account_csv(spark: Any, csv_path: str, target_table: Optional[str] = None,
                   target_path: Optional[str] = None, mode: str = "append", header: bool = True,
                   delimiter: str = ",", output_format: str = "parquet",
                   validate_required: bool = True):
    """
    Load a CSV file into the ``fintech_mobile_wallet_account`` schema and optionally append it.

    Table definition:
        Mobile wallet account table for Vodafone Cash, Orange Cash, Etisalat Cash, WE Pay, and similar Egyptian wallet products.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``.
        csv_path:
            Path to the source CSV file or folder. The CSV should contain the
            expected columns. Column matching is case-insensitive, and columns are
            reordered to match the target schema.
        target_table:
            Optional Spark table name, for example ``"golden.fintech_mobile_wallet_account"``.
            When provided, records are written using ``saveAsTable``.
        target_path:
            Optional filesystem/object-store path. When provided, records are
            written using ``DataFrameWriter.format(output_format).save``.
        mode:
            Spark write mode such as ``"append"`` or ``"overwrite"``. The default
            is ``"append"`` because this function is designed to add records.
        header:
            Whether the CSV file has a header row. Default is ``True``.
        delimiter:
            CSV delimiter. Default is comma.
        output_format:
            Storage format for ``target_path`` writes. Default is ``"parquet"``;
            use ``"delta"`` only when Delta Lake is installed in the Spark runtime.
        validate_required:
            If ``True``, missing required columns raise ``ValueError``. If
            ``False``, missing columns are added as nulls and cast to the target
            schema.

    Required columns:
        wallet_account_id, wallet_msisdn

    Output:
        ``pyspark.sql.DataFrame`` cast and ordered according to the target schema.
        If ``target_table`` or ``target_path`` is provided, the same DataFrame is
        also appended or overwritten according to ``mode``.

    Example:
        >>> df = load_fintech_mobile_wallet_account_csv(
        ...     spark,
        ...     csv_path="/mnt/landing/fintech_mobile_wallet_account.csv",
        ...     target_table="golden.fintech_mobile_wallet_account",
        ...     mode="append"
        ... )

    Agent-use hint:
        Use this function when a user uploads CSV rows to enrich a golden table.
        If the agent is unsure whether the CSV has all required columns, call with
        ``validate_required=True`` first; on failure, ask the user for the missing
        columns or retry with ``validate_required=False`` only for exploratory
        profiling.
    """
    return _load_table_csv(
        spark=spark,
        table_name="fintech_mobile_wallet_account",
        csv_path=csv_path,
        target_table=target_table,
        target_path=target_path,
        mode=mode,
        header=header,
        delimiter=delimiter,
        output_format=output_format,
        validate_required=validate_required,
    )


def generate_merchant_seller_registry(spark: Any, num_rows: int, seed: int = 42):
    """
    Generate synthetic rows for ``merchant_seller_registry`` as a PySpark DataFrame.

    Table definition:
        Business seller registry for Egyptian e-commerce marketplaces, covering tax, commercial, settlement, and contact identifiers.

    Domain and granularity:
        Domain is ``ecommerce_merchant``; granularity is ``One row per merchant legal entity or sole proprietor seller.``.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``. Agents should pass the Spark
            session already available in the execution environment.
        num_rows:
            Number of synthetic rows to create. Use small values for scanner
            smoke tests and larger values for benchmark or performance tests.
        seed:
            Deterministic seed. Keeping the same seed and row count produces
            stable golden-test records.

    Output:
        ``pyspark.sql.DataFrame`` with 11 columns, including 11
        PII-classified columns. Representative PII semantic types include:
        CUSTOMER_ID, EGYPTIAN_MOBILE, EG_COMMERCIAL_REGISTRY_NUMBER, EG_NATIONAL_REAL_ESTATE_ID, EG_TAX_REGISTRATION_NUMBER, EG_UNIFIED_NATIONAL_NUMBER, EMAIL_ADDRESS, IBAN_CODE, LOCATION_ADDRESS, PERSON.

    Example:
        >>> df = generate_merchant_seller_registry(spark, num_rows=100, seed=7)
        >>> df.write.mode("overwrite").saveAsTable("golden.merchant_seller_registry")

    Agent-use hint:
        Use this function when a workflow asks to create a clean synthetic source
        table for PII scanner comparison. The returned DataFrame is already typed
        and column-ordered according to the ODCS-style catalogue.
    """
    return _generate_table(spark, "merchant_seller_registry", num_rows=num_rows, seed=seed)


def load_merchant_seller_registry_csv(spark: Any, csv_path: str, target_table: Optional[str] = None,
                   target_path: Optional[str] = None, mode: str = "append", header: bool = True,
                   delimiter: str = ",", output_format: str = "parquet",
                   validate_required: bool = True):
    """
    Load a CSV file into the ``merchant_seller_registry`` schema and optionally append it.

    Table definition:
        Business seller registry for Egyptian e-commerce marketplaces, covering tax, commercial, settlement, and contact identifiers.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``.
        csv_path:
            Path to the source CSV file or folder. The CSV should contain the
            expected columns. Column matching is case-insensitive, and columns are
            reordered to match the target schema.
        target_table:
            Optional Spark table name, for example ``"golden.merchant_seller_registry"``.
            When provided, records are written using ``saveAsTable``.
        target_path:
            Optional filesystem/object-store path. When provided, records are
            written using ``DataFrameWriter.format(output_format).save``.
        mode:
            Spark write mode such as ``"append"`` or ``"overwrite"``. The default
            is ``"append"`` because this function is designed to add records.
        header:
            Whether the CSV file has a header row. Default is ``True``.
        delimiter:
            CSV delimiter. Default is comma.
        output_format:
            Storage format for ``target_path`` writes. Default is ``"parquet"``;
            use ``"delta"`` only when Delta Lake is installed in the Spark runtime.
        validate_required:
            If ``True``, missing required columns raise ``ValueError``. If
            ``False``, missing columns are added as nulls and cast to the target
            schema.

    Required columns:
        merchant_id

    Output:
        ``pyspark.sql.DataFrame`` cast and ordered according to the target schema.
        If ``target_table`` or ``target_path`` is provided, the same DataFrame is
        also appended or overwritten according to ``mode``.

    Example:
        >>> df = load_merchant_seller_registry_csv(
        ...     spark,
        ...     csv_path="/mnt/landing/merchant_seller_registry.csv",
        ...     target_table="golden.merchant_seller_registry",
        ...     mode="append"
        ... )

    Agent-use hint:
        Use this function when a user uploads CSV rows to enrich a golden table.
        If the agent is unsure whether the CSV has all required columns, call with
        ``validate_required=True`` first; on failure, ask the user for the missing
        columns or retry with ``validate_required=False`` only for exploratory
        profiling.
    """
    return _load_table_csv(
        spark=spark,
        table_name="merchant_seller_registry",
        csv_path=csv_path,
        target_table=target_table,
        target_path=target_path,
        mode=mode,
        header=header,
        delimiter=delimiter,
        output_format=output_format,
        validate_required=validate_required,
    )


def generate_support_ticket_free_text(spark: Any, num_rows: int, seed: int = 42):
    """
    Generate synthetic rows for ``support_ticket_free_text`` as a PySpark DataFrame.

    Table definition:
        Free-text ticket data for evaluating entity extraction from notes, SMS bodies, chat transcripts, emails, and logs.

    Domain and granularity:
        Domain is ``cross_domain_support``; granularity is ``One row per support interaction or message.``.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``. Agents should pass the Spark
            session already available in the execution environment.
        num_rows:
            Number of synthetic rows to create. Use small values for scanner
            smoke tests and larger values for benchmark or performance tests.
        seed:
            Deterministic seed. Keeping the same seed and row count produces
            stable golden-test records.

    Output:
        ``pyspark.sql.DataFrame`` with 8 columns, including 4
        PII-classified columns. Representative PII semantic types include:
        CUSTOMER_ID, FREE_TEXT.

    Example:
        >>> df = generate_support_ticket_free_text(spark, num_rows=100, seed=7)
        >>> df.write.mode("overwrite").saveAsTable("golden.support_ticket_free_text")

    Agent-use hint:
        Use this function when a workflow asks to create a clean synthetic source
        table for PII scanner comparison. The returned DataFrame is already typed
        and column-ordered according to the ODCS-style catalogue.
    """
    return _generate_table(spark, "support_ticket_free_text", num_rows=num_rows, seed=seed)


def load_support_ticket_free_text_csv(spark: Any, csv_path: str, target_table: Optional[str] = None,
                   target_path: Optional[str] = None, mode: str = "append", header: bool = True,
                   delimiter: str = ",", output_format: str = "parquet",
                   validate_required: bool = True):
    """
    Load a CSV file into the ``support_ticket_free_text`` schema and optionally append it.

    Table definition:
        Free-text ticket data for evaluating entity extraction from notes, SMS bodies, chat transcripts, emails, and logs.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``.
        csv_path:
            Path to the source CSV file or folder. The CSV should contain the
            expected columns. Column matching is case-insensitive, and columns are
            reordered to match the target schema.
        target_table:
            Optional Spark table name, for example ``"golden.support_ticket_free_text"``.
            When provided, records are written using ``saveAsTable``.
        target_path:
            Optional filesystem/object-store path. When provided, records are
            written using ``DataFrameWriter.format(output_format).save``.
        mode:
            Spark write mode such as ``"append"`` or ``"overwrite"``. The default
            is ``"append"`` because this function is designed to add records.
        header:
            Whether the CSV file has a header row. Default is ``True``.
        delimiter:
            CSV delimiter. Default is comma.
        output_format:
            Storage format for ``target_path`` writes. Default is ``"parquet"``;
            use ``"delta"`` only when Delta Lake is installed in the Spark runtime.
        validate_required:
            If ``True``, missing required columns raise ``ValueError``. If
            ``False``, missing columns are added as nulls and cast to the target
            schema.

    Required columns:
        ticket_id

    Output:
        ``pyspark.sql.DataFrame`` cast and ordered according to the target schema.
        If ``target_table`` or ``target_path`` is provided, the same DataFrame is
        also appended or overwritten according to ``mode``.

    Example:
        >>> df = load_support_ticket_free_text_csv(
        ...     spark,
        ...     csv_path="/mnt/landing/support_ticket_free_text.csv",
        ...     target_table="golden.support_ticket_free_text",
        ...     mode="append"
        ... )

    Agent-use hint:
        Use this function when a user uploads CSV rows to enrich a golden table.
        If the agent is unsure whether the CSV has all required columns, call with
        ``validate_required=True`` first; on failure, ask the user for the missing
        columns or retry with ``validate_required=False`` only for exploratory
        profiling.
    """
    return _load_table_csv(
        spark=spark,
        table_name="support_ticket_free_text",
        csv_path=csv_path,
        target_table=target_table,
        target_path=target_path,
        mode=mode,
        header=header,
        delimiter=delimiter,
        output_format=output_format,
        validate_required=validate_required,
    )


def generate_sensitive_customer_risk_profile(spark: Any, num_rows: int, seed: int = 42):
    """
    Generate synthetic rows for ``sensitive_customer_risk_profile`` as a PySpark DataFrame.

    Table definition:
        Synthetic table for high-risk sensitive personal data classes under privacy and sector controls. Use only synthetic values.

    Domain and granularity:
        Domain is ``cross_domain_sensitive``; granularity is ``One row per customer risk or eligibility assessment snapshot.``.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``. Agents should pass the Spark
            session already available in the execution environment.
        num_rows:
            Number of synthetic rows to create. Use small values for scanner
            smoke tests and larger values for benchmark or performance tests.
        seed:
            Deterministic seed. Keeping the same seed and row count produces
            stable golden-test records.

    Output:
        ``pyspark.sql.DataFrame`` with 9 columns, including 8
        PII-classified columns. Representative PII semantic types include:
        BIOMETRIC_DATA, CHILDREN_DATA, CUSTOMER_ID, EG_CRIMINAL_RECORD, HEALTH_DATA, MEDICAL_RECORD_NUMBER, NRP, RELIGION.

    Example:
        >>> df = generate_sensitive_customer_risk_profile(spark, num_rows=100, seed=7)
        >>> df.write.mode("overwrite").saveAsTable("golden.sensitive_customer_risk_profile")

    Agent-use hint:
        Use this function when a workflow asks to create a clean synthetic source
        table for PII scanner comparison. The returned DataFrame is already typed
        and column-ordered according to the ODCS-style catalogue.
    """
    return _generate_table(spark, "sensitive_customer_risk_profile", num_rows=num_rows, seed=seed)


def load_sensitive_customer_risk_profile_csv(spark: Any, csv_path: str, target_table: Optional[str] = None,
                   target_path: Optional[str] = None, mode: str = "append", header: bool = True,
                   delimiter: str = ",", output_format: str = "parquet",
                   validate_required: bool = True):
    """
    Load a CSV file into the ``sensitive_customer_risk_profile`` schema and optionally append it.

    Table definition:
        Synthetic table for high-risk sensitive personal data classes under privacy and sector controls. Use only synthetic values.

    Inputs:
        spark:
            Active ``pyspark.sql.SparkSession``.
        csv_path:
            Path to the source CSV file or folder. The CSV should contain the
            expected columns. Column matching is case-insensitive, and columns are
            reordered to match the target schema.
        target_table:
            Optional Spark table name, for example ``"golden.sensitive_customer_risk_profile"``.
            When provided, records are written using ``saveAsTable``.
        target_path:
            Optional filesystem/object-store path. When provided, records are
            written using ``DataFrameWriter.format(output_format).save``.
        mode:
            Spark write mode such as ``"append"`` or ``"overwrite"``. The default
            is ``"append"`` because this function is designed to add records.
        header:
            Whether the CSV file has a header row. Default is ``True``.
        delimiter:
            CSV delimiter. Default is comma.
        output_format:
            Storage format for ``target_path`` writes. Default is ``"parquet"``;
            use ``"delta"`` only when Delta Lake is installed in the Spark runtime.
        validate_required:
            If ``True``, missing required columns raise ``ValueError``. If
            ``False``, missing columns are added as nulls and cast to the target
            schema.

    Required columns:
        risk_profile_id

    Output:
        ``pyspark.sql.DataFrame`` cast and ordered according to the target schema.
        If ``target_table`` or ``target_path`` is provided, the same DataFrame is
        also appended or overwritten according to ``mode``.

    Example:
        >>> df = load_sensitive_customer_risk_profile_csv(
        ...     spark,
        ...     csv_path="/mnt/landing/sensitive_customer_risk_profile.csv",
        ...     target_table="golden.sensitive_customer_risk_profile",
        ...     mode="append"
        ... )

    Agent-use hint:
        Use this function when a user uploads CSV rows to enrich a golden table.
        If the agent is unsure whether the CSV has all required columns, call with
        ``validate_required=True`` first; on failure, ask the user for the missing
        columns or retry with ``validate_required=False`` only for exploratory
        profiling.
    """
    return _load_table_csv(
        spark=spark,
        table_name="sensitive_customer_risk_profile",
        csv_path=csv_path,
        target_table=target_table,
        target_path=target_path,
        mode=mode,
        header=header,
        delimiter=delimiter,
        output_format=output_format,
        validate_required=validate_required,
    )


GENERATOR_FUNCTIONS = {
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

CSV_LOADER_FUNCTIONS = {
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


def generate_table(spark: Any, table_name: str, num_rows: int, seed: int = 42):
    """Generic dispatcher for agents that receive table names dynamically."""
    if table_name not in GENERATOR_FUNCTIONS:
        raise KeyError(f"Unknown table_name={table_name!r}. Expected one of: {sorted(GENERATOR_FUNCTIONS)}")
    return GENERATOR_FUNCTIONS[table_name](spark=spark, num_rows=num_rows, seed=seed)


def load_table_csv(spark: Any, table_name: str, csv_path: str, **kwargs: Any):
    """Generic CSV-loader dispatcher for agents that receive table names dynamically."""
    if table_name not in CSV_LOADER_FUNCTIONS:
        raise KeyError(f"Unknown table_name={table_name!r}. Expected one of: {sorted(CSV_LOADER_FUNCTIONS)}")
    return CSV_LOADER_FUNCTIONS[table_name](spark=spark, csv_path=csv_path, **kwargs)
