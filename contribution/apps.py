from django.apps import AppConfig
import inspect, importlib

MODULE_NAME = "contribution"

DEFAULT_CFG = {
    "gql_query_premiums_perms": ["101301"],
    "gql_mutation_create_premiums_perms": ["101302"],
    "gql_mutation_update_premiums_perms": ["101303"],
    "gql_mutation_delete_premiums_perms": ["101304"],
    "generate_invoice_on_contribution": False
}

CALCULATION_RULES = []

def read_all_calculation_rules():
    """function to read all calculation rules"""
    for name, cls in inspect.getmembers(importlib.import_module("calculation_comores.calculation_rule"), inspect.isclass):
        if 'calculation' in cls.__module__.split('.')[0]:
            CALCULATION_RULES.append(cls)
            cls.ready()

class ContributionConfig(AppConfig):
    name = MODULE_NAME

    gql_query_premiums_perms = []
    gql_mutation_create_premiums_perms = []
    gql_mutation_update_premiums_perms = []
    gql_mutation_delete_premiums_perms = []
    generate_invoice_on_contribution = None

    def __load_config(self, cfg):
        for field in cfg:
            if hasattr(ContributionConfig, field):
                setattr(ContributionConfig, field, cfg[field])

    def ready(self):
        from core.models import ModuleConfiguration
        cfg = ModuleConfiguration.get_or_default(MODULE_NAME, DEFAULT_CFG)
        self.__load_config(cfg)
        read_all_calculation_rules()
