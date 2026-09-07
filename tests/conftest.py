import os

os.environ.setdefault("FINOPS_CONTROL_PROJECT_ID", "test-control")
os.environ.setdefault("FINOPS_TRACE_TO_CLOUD", "false")
os.environ.setdefault("FINOPS_VERIFY_PUBSUB_TOKENS", "false")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-control")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
