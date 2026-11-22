"""Models available for account assignments."""

DEFAULT_MODEL = '5814-domestic'
MODELS = {
    DEFAULT_MODEL: ('5814 국내주식 ATS', frozenset({'kiwoom', 'kis'})),
}


def available_models(broker):
    return {model_id: label for model_id, (label, brokers) in MODELS.items() if broker in brokers}


def require_kis_model(model_id):
    # The existing KIS worker implements the 5814 engine only.
    if model_id not in ('', DEFAULT_MODEL):
        raise ValueError('KIS_ATS_MODEL_UNSUPPORTED')
