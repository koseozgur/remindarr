"""Optional AI reading of reminder messages the grammar can't parse.

A plugin in the brief's sense: off unless a provider credential is set, and
provider-agnostic. The model never acts on its own. It fills in a structured
:class:`~remindarr.ai.schema.Reading`; code validates that and turns it into
the same ``ParsedReminder`` the grammar produces, repeats included.
"""
