"""cloud_pack — generador READ-ONLY del pack de cobertura CLOUD POSTURE (CSPM) para qley21719.

Motor SEPARADO del de Policy Compliance (compliance_pack/). NO emite policy.xml ni muta el
tenant: lee posture CSPM (controls + evaluations) vía qualys_client.CloudViewClient (solo GET,
allow-list-only) y emite un mapping report (control cloud -> familia legal -> artículo, con
PASS/FAIL) + gaps + apply-instructions. El cliente aplica por UI (human-gate). Ver
DESIGN-cloud-posture.md y mapping/ley21719-cloud.yaml.
"""
from .generator import (classify_control, parse_controls, parse_evaluations,
                        parse_resource_counts, build_pack)

__all__ = ["classify_control", "parse_controls", "parse_evaluations",
           "parse_resource_counts", "build_pack"]
