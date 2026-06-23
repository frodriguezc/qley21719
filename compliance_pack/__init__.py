"""compliance_pack — generador READ-ONLY de packs de compliance para qley21719.

Convierte una ley (mapeada en un spec YAML) en un Policy XML importable de Qualys PC,
cosechando controles de policies de libreria del tenant. NO muta el tenant: solo lee
(export de policies + catalogo de controles) via qualys_client y emite archivos. El
import lo corre el cliente (human-gate), fuera de la herramienta.
"""
from .generator import generate_pack

__all__ = ["generate_pack"]
