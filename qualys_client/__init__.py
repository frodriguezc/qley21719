"""Cliente HTTP read-only de la API de Qualys (implementación propia e independiente).

Dos motores read-only:
  - QualysClient  — Policy Compliance host-based (XML API api/2.0|4.0/fo + QPS).
  - CloudViewClient — CSPM cloud posture (REST cloudview-api/rest/v1), allow-list-only.
"""
from .client import QualysClient, QualysReadOnlyError, PODS, from_env
from .cloudview import CloudViewClient, from_env as cloudview_from_env

__all__ = [
    "QualysClient", "QualysReadOnlyError", "PODS", "from_env",
    "CloudViewClient", "cloudview_from_env",
]
