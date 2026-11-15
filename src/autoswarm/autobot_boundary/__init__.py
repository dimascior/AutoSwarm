"""AutoSwarm's side of the Autobot execution boundary.

Consumes the canonical ExecutionManifest that Autobot places in the
call directory before launch. Verifies the manifest at claim time
and writes the ExecutionClaimRecord — the durable statement of what
AutoSwarm accepted before starting the provider.
"""

from .verify import verify_manifest
from .claim import write_claim_record
