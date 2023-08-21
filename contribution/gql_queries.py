import graphene
from graphene_django import DjangoObjectType
from .models import Premium, PremiumMutation
from core import prefix_filterset, ExtendedConnection
from policy.schema import PolicyGQLType


class PremiumGQLType(DjangoObjectType):
    client_mutation_id = graphene.String()

    class Meta:
        model = Premium
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "uuid": ["exact"],
            "amount": ["exact", "lt", "lte", "gt", "gte"],
            "pay_date": ["exact", "lt", "lte", "gt", "gte"],
            "pay_type": ["exact"],
            "payment_number": ["exact"],
            "network_operator": ["exact"],
            "is_photo_fee": ["exact"],
            "receipt": ["exact", "icontains"],
            **prefix_filterset("policy__", PolicyGQLType._meta.filter_fields)
        }
        connection_class = ExtendedConnection

    def resolve_client_mutation_id(self, info):
        premium_mutation = self.mutations.select_related(
            'mutation').filter(mutation__status=0).first()
        return premium_mutation.mutation.client_mutation_id if premium_mutation else None


class PremiumMutationGQLType(DjangoObjectType):
    class Meta:
        model = PremiumMutation
