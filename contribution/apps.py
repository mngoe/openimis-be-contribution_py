from django.apps import AppConfig

MODULE_NAME = "contribution"

DEFAULT_CFG = {
    "gql_query_premiums_perms": ["101301"],
    "gql_mutation_create_premiums_perms": ["101302"],
    "gql_mutation_update_premiums_perms": ["101303"],
    "gql_mutation_delete_premiums_perms": ["101304"],
    "generate_invoice_on_contribution": False,
    "number_of_invoice_on_contribution": 1
}


class ContributionConfig(AppConfig):
    name = MODULE_NAME

    gql_query_premiums_perms = []
    gql_mutation_create_premiums_perms = []
    gql_mutation_update_premiums_perms = []
    gql_mutation_delete_premiums_perms = []
    generate_invoice_on_contribution = None
    number_of_invoice_on_contribution = None

    def __load_config(self, cfg):
        for field in cfg:
            if hasattr(ContributionConfig, field):
                setattr(ContributionConfig, field, cfg[field])

    def ready(self):
        from core.models import ModuleConfiguration
        cfg = ModuleConfiguration.get_or_default(MODULE_NAME, DEFAULT_CFG)
        self.__load_config(cfg)
