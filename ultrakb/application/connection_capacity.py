"""Sourced, model-bound capabilities for supported connection examples."""

from ultrakb.model_capabilities import connection_capacity


def template_capacity(template):
    return connection_capacity(template.model, template.base_url)
