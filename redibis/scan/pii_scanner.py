"""Back-compat re-export — ``PIIScanner`` lives in ``redibis.pii.scanner``."""

from redibis.pii.scanner import PIIScanner, pii_scanner_from_redibis

__all__ = ["PIIScanner", "pii_scanner_from_redibis"]
