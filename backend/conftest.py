import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# Fixed demo identity for tests (a .test address counts as deliverable; example.com is skipped on purpose).
os.environ.update({"VOICE_AGENT_DEMO_NAME": "Jordan Lee", "VOICE_AGENT_DEMO_PHONE": "+16505550100",
                   "VOICE_AGENT_DEMO_EMAIL": "jordan@demo-clinic.test", "VOICE_AGENT_PHONE_ALLOW": "+16505550100"})
