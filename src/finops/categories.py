"""Spend categorization: turn a provider + service line into one of six buckets.

Pure and table-driven. No DB, no network, no clock. `classify_category` is the
one entry point the ingest paths call per row; everything else here is the table
it reads. Keep it dispatch -> table lookup -> GPU check so the branch count stays
low and both CUR aggregators (duckdb and pyarrow) get an identical answer for the
same row.

The point of the module: AI and GPU spend is a real number the customer can see,
not a guess. A managed AI service (SageMaker, Bedrock, Vertex) is AI. A GPU
instance running under plain EC2 is AI. A model provider bill (OpenAI, Anthropic)
is AI. A Savings Plan or Reserved Instance recurring fee is compute, never a GPU
guess, because the fee line carries no real usage to read.
"""
from __future__ import annotations

import re

# The six buckets every dollar lands in. Order is the render order downstream.
CATEGORY_KEYS = ("ai", "compute", "data", "storage", "network", "other")

# Standalone model / inference providers. A bill from any of these is AI by
# definition, whatever the service string says. Pinned to the connector set nable
# ships; cloud-native managed AI (Bedrock, Vertex) is caught by the service table
# below, not here.
LLM_PROVIDERS = frozenset({
    "openai", "anthropic", "openrouter", "litellm", "together", "replicate",
    "modal", "cohere", "mistral", "huggingface", "perplexity",
})

# GPU / accelerator families. Documented set; the regex below is what actually
# matches, reused from the production instance-type recognizer so the two cannot
# drift. p/g = GPU, trn/inf = Trainium/Inferentia, dl = Deep Learning.
GPU_FAMILIES = frozenset({"p", "g", "trn", "inf", "dl"})

# Anchored to a size suffix so it matches p4d.24xlarge / g5.2xlarge / inf2.xlarge
# inside a bare instance type OR inside a usage type ("BoxUsage:p4d.24xlarge"),
# and never matches a storage class ("s3.standard") or a version ("v2.0").
_GPU_RE = re.compile(r"\b(?:p\d|g\d|inf\d|trn\d|dl\d)[a-z]*\.", re.IGNORECASE)

# A Savings Plan / Reserved Instance FEE line: pure commitment amortization with
# no usage to read. Covered-USAGE lines are excluded on purpose, they carry the
# real instance and classify by GPU like any other usage.
_COMMITMENT_FEE_RE = re.compile(r"recurringfee|upfrontfee|rifee", re.IGNORECASE)


def _normalise_token(service: str | None) -> str:
    """Collapse a service name to one comparable token.

    "Amazon SageMaker" and "AmazonSageMaker" both become "amazonsagemaker", so the
    table can be keyed once and hit by either the human name or the service code.
    """
    return re.sub(r"[^a-z0-9]+", "", (service or "").lower())


# ── Service -> category taxonomy ─────────────────────────────────────────────
# Keyed on the normalised token. Both the long product name and the short service
# code are listed for services where they differ ("amazonelasticcomputecloud" and
# "amazonec2"), because the CUR ships either depending on format and era.
SERVICE_CATEGORY: dict[str, str] = {
    # ── AI / ML (managed) ──
    "amazonsagemaker": "ai",
    "amazonbedrock": "ai",
    "amazoncomprehend": "ai",
    "amazonrekognition": "ai",
    "amazonpolly": "ai",
    "amazontranscribe": "ai",
    "amazontextract": "ai",
    "amazonlex": "ai",
    "amazonkendra": "ai",
    "amazonpersonalize": "ai",
    "amazonforecast": "ai",
    "amazontranslate": "ai",
    "amazonaugmentedairuntime": "ai",
    "amazonq": "ai",
    # GCP managed AI
    "vertexai": "ai",
    "vertexaiapi": "ai",
    "cloudmachinelearningengine": "ai",
    "cloudnaturallanguage": "ai",
    "cloudvision": "ai",
    "cloudspeech": "ai",
    "cloudspeechtotext": "ai",
    "cloudtranslation": "ai",
    "notebooks": "ai",
    # ── Compute ──
    "amazonec2": "compute",
    "amazonelasticcomputecloud": "compute",
    "amazonelasticcomputecloudcompute": "compute",
    "awslambda": "compute",
    "amazonecs": "compute",
    "amazoneks": "compute",
    "awsfargate": "compute",
    "amazonlightsail": "compute",
    "awsbatch": "compute",
    "awselasticbeanstalk": "compute",
    "amazonec2containerregistry": "compute",
    "computeengine": "compute",
    "kubernetesengine": "compute",
    "cloudrun": "compute",
    "cloudfunctions": "compute",
    "appengine": "compute",
    # ── Storage ──
    "amazons3": "storage",
    "amazonsimplestorageservice": "storage",
    "amazonebs": "storage",
    "amazonelasticblockstore": "storage",
    "amazonefs": "storage",
    "amazonfsx": "storage",
    "amazonglacier": "storage",
    "amazons3glacierdeeparchive": "storage",
    "awsbackup": "storage",
    "awsstoragegateway": "storage",
    "cloudstorage": "storage",
    "persistentdisk": "storage",
    "filestore": "storage",
    # ── Data / analytics / databases ──
    "amazonrds": "data",
    "amazonrelationaldatabaseservice": "data",
    "amazondynamodb": "data",
    "amazonredshift": "data",
    "amazonelasticache": "data",
    "amazonathena": "data",
    "awsglue": "data",
    "amazonemr": "data",
    "amazonkinesis": "data",
    "amazonmsk": "data",
    "amazonopensearchservice": "data",
    "amazonelasticsearchservice": "data",
    "amazonaurora": "data",
    "amazonneptune": "data",
    "amazondocumentdb": "data",
    "amazonquicksight": "data",
    "amazontimestream": "data",
    "bigquery": "data",
    "cloudsql": "data",
    "cloudbigtable": "data",
    "cloudspanner": "data",
    "clouddatastore": "data",
    "firestore": "data",
    "clouddataflow": "data",
    "clouddataproc": "data",
    "cloudpubsub": "data",
    "cloudcomposer": "data",
    # ── Network ──
    "amazoncloudfront": "network",
    "amazonvpc": "network",
    "amazonvirtualprivatecloud": "network",
    "awsdatatransfer": "network",
    "elasticloadbalancing": "network",
    "awselasticloadbalancing": "network",
    "amazonroute53": "network",
    "amazonapigateway": "network",
    "awsdirectconnect": "network",
    "awsglobalaccelerator": "network",
    "cloudcdn": "network",
    "cloudloadbalancing": "network",
    "clouddns": "network",
    "networking": "network",
}

# ── Plain-English labels (no service codes leak to a reader) ─────────────────
CATEGORY_LABELS: dict[str, str] = {
    "ai": "AI and GPU",
    "compute": "Compute",
    "data": "Data and analytics",
    "storage": "Storage",
    "network": "Networking",
    "other": "Other",
}

AI_KIND_LABELS: dict[str, str] = {
    "managed_ai": "Managed AI services",
    "accelerator_compute": "GPU and accelerators",
    "model_provider": "Model providers",
}

# Plain-English names for the AI lines a breakdown surfaces.
PROVIDER_LABELS: dict[str, str] = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "openrouter": "OpenRouter",
    "litellm": "LiteLLM",
    "together": "Together AI",
    "replicate": "Replicate",
    "modal": "Modal",
    "cohere": "Cohere",
    "mistral": "Mistral",
    "huggingface": "Hugging Face",
    "perplexity": "Perplexity",
}

AI_SERVICE_LABELS: dict[str, str] = {
    "amazonsagemaker": "SageMaker",
    "amazonbedrock": "Bedrock",
    "amazoncomprehend": "Comprehend",
    "amazonrekognition": "Rekognition",
    "amazonq": "Amazon Q",
    "amazonec2": "GPU compute",
    "amazonelasticcomputecloud": "GPU compute",
    "vertexai": "Vertex AI",
    "vertexaiapi": "Vertex AI",
    "computeengine": "GPU compute",
}


def _provider_is_ai(provider: str | None) -> bool:
    return (provider or "").strip().lower() in LLM_PROVIDERS


def _is_commitment_fee(usage_type: str | None) -> bool:
    """True for an SP/RI recurring or upfront fee line (no usage to read)."""
    return bool(usage_type) and bool(_COMMITMENT_FEE_RE.search(usage_type))


def _is_gpu(usage_type: str | None, instance_type: str | None) -> bool:
    for tok in (instance_type, usage_type):
        if tok and _GPU_RE.search(tok):
            return True
    return False


def classify_category(
    provider: str,
    service: str,
    usage_type: str | None = None,
    instance_type: str | None = None,
) -> str:
    """One of CATEGORY_KEYS for a single spend line.

    Order matters: a model-provider bill is AI outright; an SP/RI fee line is
    compute before any GPU guess runs; a managed AI service is AI; a GPU instance
    under plain compute is AI; everything else falls to its table category.
    """
    if _provider_is_ai(provider):
        return "ai"
    if _is_commitment_fee(usage_type):
        return "compute"
    base = SERVICE_CATEGORY.get(_normalise_token(service), "other")
    if base == "ai":
        return "ai"
    if _is_gpu(usage_type, instance_type):
        return "ai"
    return base


def is_ai(key: str) -> bool:
    return key == "ai"


def ai_kind(provider: str, service: str) -> str:
    """managed_ai | accelerator_compute | model_provider for an AI line."""
    if _provider_is_ai(provider):
        return "model_provider"
    if SERVICE_CATEGORY.get(_normalise_token(service)) == "ai":
        return "managed_ai"
    return "accelerator_compute"


def ai_label(provider: str, service: str) -> str:
    """Plain-English name for an AI breakdown row. Never a raw service code."""
    p = (provider or "").strip().lower()
    if p in PROVIDER_LABELS:
        return PROVIDER_LABELS[p]
    token = _normalise_token(service)
    if token in AI_SERVICE_LABELS:
        return AI_SERVICE_LABELS[token]
    return service or "AI"
