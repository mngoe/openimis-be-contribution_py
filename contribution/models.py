import uuid

from core import fields
from core import models as core_models
from django.db import models
from django.utils.translation import gettext_lazy
from policy.models import Policy
from payer.models import Payer


class PayTypeChoices(models.TextChoices):
    BANK_TRANSFER = "B", gettext_lazy("Bank transfer")
    CASH = "C", gettext_lazy("Cash")
    MOBILE = "M", gettext_lazy("Mobile phone")
    FUNDING = "F", gettext_lazy("Funding")


class Premium(core_models.VersionedModel):
    id = models.AutoField(db_column="PremiumId", primary_key=True)
    uuid = models.CharField(
        db_column="PremiumUUID", max_length=36, default=uuid.uuid4, unique=True
    )
    policy = models.ForeignKey(
        Policy, models.DO_NOTHING, db_column="PolicyID", related_name="premiums"
    )
    payer = models.ForeignKey(
        Payer,
        models.DO_NOTHING,
        db_column="PayerID",
        blank=True,
        null=True,
        related_name="premiums",
    )
    amount = models.DecimalField(db_column="Amount", max_digits=18, decimal_places=2)
    receipt = models.CharField(db_column="Receipt", max_length=50)
    pay_date = fields.DateField(db_column="PayDate")
    pay_type = models.CharField(
        db_column="PayType", max_length=1
    )  # , choices=PayTypeChoices.choices
    is_photo_fee = models.BooleanField(
        db_column="isPhotoFee", blank=True, null=True, default=False
    )
    is_offline = models.BooleanField(
        db_column="isOffline", blank=True, null=True, default=False
    )
    reporting_id = models.IntegerField(db_column="ReportingId", blank=True, null=True)
    audit_user_id = models.IntegerField(db_column="AuditUserID")
    # rowid = models.TextField(db_column='RowID', blank=True, null=True)
    payment_number = models.CharField(db_column="paymentNumber", max_length=20, blank=True, null=True)
    network_operator = models.CharField(db_column="networkOperator", max_length=1, blank=True, null=True)

    class Meta:
        managed = True
        db_table = 'tblPremium'


class PremiumMutation(core_models.UUIDModel, core_models.ObjectMutation):
    premium = models.ForeignKey(Premium, models.DO_NOTHING, related_name='mutations')
    mutation = models.ForeignKey(core_models.MutationLog, models.DO_NOTHING, related_name='premiums')

    class Meta:
        managed = True
        db_table = "contribution_PremiumMutation"
