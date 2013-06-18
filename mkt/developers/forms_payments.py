from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

import commonware
import happyforms
from tower import ugettext as _, ugettext_lazy as _lazy

import amo
from amo.utils import raise_required
from addons.models import Addon, AddonUpsell
from constants.payments import (PAYMENT_METHOD_OPERATOR,
                                PAYMENT_METHOD_CARD,
                                PAYMENT_METHOD_ALL)
from editors.models import RereviewQueue
from market.models import AddonPremium, Price

from mkt.constants import (BANGO_COUNTRIES, BANGO_OUTPAYMENT_CURRENCIES,
                           FREE_PLATFORMS, PAID_PLATFORMS)
from mkt.site.forms import AddonChoiceField
from mkt.submit.forms import DeviceTypeForm

from .models import AddonPaymentAccount, PaymentAccount


log = commonware.log.getLogger('z.devhub')


def _restore_app(app, save=True):
    """
    Restore an incomplete app to its former status. The app will be marked
    as its previous status or PENDING if it was never reviewed.
    """

    log.info('Changing app from incomplete to previous status: %d' % app.pk)
    app.status = (app.highest_status if
                  app.highest_status != amo.STATUS_NULL else
                  amo.STATUS_PENDING)
    if save:
        app.save()


class PremiumForm(DeviceTypeForm, happyforms.Form):
    """
    The premium details for an addon, which is unfortunately
    distributed across a few models.
    """

    # This does a nice Yes/No field like the mockup calls for.
    allow_inapp = forms.ChoiceField(
        choices=((True, _lazy(u'Yes')), (False, _lazy(u'No'))),
        widget=forms.RadioSelect, required=False)
    # Choices are provided at init by group_tier_choices.
    price = forms.ChoiceField(choices=(), label=_lazy(u'App Price'),
                              required=False)

    def __init__(self, *args, **kw):
        self.request = kw.pop('request')
        self.addon = kw.pop('addon')
        self.user = kw.pop('user')

        kw['initial'] = {
            'allow_inapp': self.addon.premium_type in amo.ADDON_INAPPS
        }
        if self.addon.premium and self.addon.premium.price:
            # If the app has a premium object, set the initial price.
            kw['initial']['price'] = self.addon.premium.price.pk

        super(PremiumForm, self).__init__(*args, **kw)

        if (self.addon.premium_type in amo.ADDON_PREMIUMS
                and not self.is_toggling()):
            # Require the price field if the app is premium and
            # we're not toggling from free <-> paid.
            self.fields['price'].required = True

        # Get the list of supported devices and put them in the data.
        self.device_data = {}
        supported_devices = [amo.REVERSE_DEVICE_LOOKUP[dev.id] for dev in
                             self.addon.device_types]
        self.initial.setdefault('free_platforms', [])
        self.initial.setdefault('paid_platforms', [])

        for platform in set(x[0].split('-', 1)[1] for x in
                            FREE_PLATFORMS + PAID_PLATFORMS):
            supported = platform in supported_devices
            self.device_data['free-%s' % platform] = supported
            self.device_data['paid-%s' % platform] = supported

            if supported:
                self.initial['free_platforms'].append('free-%s' % platform)
                self.initial['paid_platforms'].append('paid-%s' % platform)

        if not self.initial.get('price'):
            self.initial['price'] = self._initial_price_id()

        self.fields['price'].choices = self.group_tier_choices()

    def group_tier_choices(self):
        """Creates tier choices with optgroups based on payment methods"""
        price_choices = [('', _('Please select a price'))]
        card_billed = []
        operator_billed = []
        card_and_operator_billed = []

        for price in Price.objects.active():
            choice = (price.pk, unicode(price))
            # Special case zero priced tier.
            if price.price == Decimal('0.00'):
                price_choices.append((price.pk, _lazy('Free')))
            # Tiers that can only be operator billed.
            elif price.method == PAYMENT_METHOD_OPERATOR:
                operator_billed.append(choice)
            # Tiers that can only be card billed.
            elif price.method == PAYMENT_METHOD_CARD:
                card_billed.append(choice)
            # Tiers that are can generally be billed by either
            # operator or card.
            elif price.method == PAYMENT_METHOD_ALL:
                card_and_operator_billed.append(choice)

        if operator_billed:
            price_choices.append((_lazy('Only supports carrier billing'),
                                  operator_billed))
        if card_billed:
            price_choices.append((_lazy('Only supports credit-card billing'),
                                  card_billed))
        if card_and_operator_billed:
            price_choices.append(
                (_lazy('Supports carrier billing and credit cards'),
                 card_and_operator_billed))

        return price_choices

    def _initial_price_id(self):
        """Sets the inital price tier if available."""
        try:
            return Price.objects.active().get(price='0.99').id
        except Price.DoesNotExist:
            log.warning('Could not find a price tier 0.99 to set as default.')
            return None

    def _make_premium(self):
        if self.addon.premium:
            return self.addon.premium

        log.info('New AddonPremium object for addon %s' % self.addon.pk)
        return AddonPremium(addon=self.addon,
                            price_id=self._initial_price_id())

    def is_paid(self):
        return self.addon.premium_type in amo.ADDON_PREMIUMS

    def is_toggling(self):
        value = self.request.POST.get('toggle-paid')
        return value if value in ('free', 'paid') else False

    def clean(self):

        is_toggling = self.is_toggling()

        if self.addon.is_packaged and 'desktop' in self._get_combined():
           self._errors['free_platforms'] = self._errors['paid_platforms'] = (
                self.ERRORS['packaged'])

        elif not is_toggling:
            # If a platform wasn't selected, raise an error.
            if not self.cleaned_data[
                '%s_platforms' % ('paid' if self.is_paid() else 'free')]:

                self._add_error('none')

                # We want to throw out the user's selections in this case and
                # not update the <select> element that goes along with this.
                # I.e.: we don't want to re-populate these big chunky
                # checkboxes with bad data.
                # Also, I'm so, so sorry.
                self.data = dict(self.data)
                platforms = dict(
                    free_platforms=self.initial.get('free_platforms', []),
                    paid_platforms=self.initial.get('paid_platforms', []))
                self.data.update(**platforms)

        return self.cleaned_data

    def clean_price(self):

        price_id = self.cleaned_data['price']
        if (self.cleaned_data.get('premium_type') in amo.ADDON_PREMIUMS
                and not price_id and not self.is_toggling()):
            raise_required()

        if not price_id and self.fields['price'].required is False:
            return None

        try:
            price = Price.objects.get(pk=price_id, active=True)
        except (ValueError, Price.DoesNotExist):
            raise ValidationError(
                self.fields['price'].error_messages['invalid_choice'])

        return price

    def save(self):
        toggle = self.is_toggling()
        upsell = self.addon.upsold
        is_premium = self.is_paid()

        if toggle == 'paid' and self.addon.premium_type == amo.ADDON_FREE:
            # Toggle free apps to paid by giving them a premium object.

            premium = self._make_premium()
            premium.price_id = self._initial_price_id()
            premium.save()

            self.addon.premium_type = amo.ADDON_PREMIUM
            self.addon.status = amo.STATUS_NULL

            is_premium = True

        elif toggle == 'free' and is_premium:
            # If the app is paid and we're making it free, remove it as an
            # upsell (if an upsell exists).
            upsell = self.addon.upsold
            if upsell:
                log.debug('[1@%s] Removing upsell; switching to free' %
                              self.addon.pk)
                upsell.delete()

            log.debug('[1@%s] Removing app payment account' % self.addon.pk)
            AddonPaymentAccount.objects.filter(addon=self.addon).delete()

            log.debug('[1@%s] Setting app premium_type to FREE' %
                          self.addon.pk)
            self.addon.premium_type = amo.ADDON_FREE

            if self.addon.status == amo.STATUS_NULL:
                _restore_app(self.addon, save=False)

            is_premium = False

        elif is_premium:
            # The dev is submitting updates for payment data about a paid app.
            # This might also happen if she is associating a new paid app
            # with an existing bank account.
            premium = self._make_premium()
            self.addon.premium_type = (
                amo.ADDON_PREMIUM_INAPP if
                self.cleaned_data.get('allow_inapp') == 'True' else
                amo.ADDON_PREMIUM)

            if 'price' in self.cleaned_data:
                log.debug('[1@%s] Updating app price (%s)' %
                          (self.addon.pk, self.cleaned_data['price']))
                premium.price = self.cleaned_data['price']

            premium.save()

        if not toggle:
            # Save the device compatibility information when we're not
            # toggling.
            super(PremiumForm, self).save(self.addon, is_premium)

        log.info('Saving app payment changes for addon %s.' % self.addon.pk)
        self.addon.save()


class UpsellForm(happyforms.Form):
    upsell_of = AddonChoiceField(queryset=Addon.objects.none(), required=False,
                                 label=_lazy(u'This is a paid upgrade of'),
                                 empty_label=_lazy(u'Not an upgrade'))

    def __init__(self, *args, **kw):
        self.addon = kw.pop('addon')
        self.user = kw.pop('user')

        kw.setdefault('initial', {})
        if self.addon.upsold:
            kw['initial']['upsell_of'] = self.addon.upsold.free

        super(UpsellForm, self).__init__(*args, **kw)

        self.fields['upsell_of'].queryset = (
            self.user.addons.exclude(pk=self.addon.pk,
                                     status=amo.STATUS_DELETED)
                            .filter(premium_type__in=amo.ADDON_FREES,
                                    type=self.addon.type))

    def save(self):
        current_upsell = self.addon.upsold
        new_upsell_app = self.cleaned_data.get('upsell_of')

        if new_upsell_app:
            # We're changing the upsell or creating a new one.

            if not current_upsell:
                # If the upsell is new or we just deleted the old upsell,
                # create a new upsell.
                log.debug('[1@%s] Creating app upsell' % self.addon.pk)
                current_upsell = AddonUpsell(premium=self.addon)

            # Set the upsell object to point to the app that we're upselling.
            current_upsell.free = new_upsell_app
            current_upsell.save()

        elif current_upsell:
            # We're deleting the upsell.
            log.debug('[1@%s] Deleting the app upsell' % self.addon.pk)
            current_upsell.delete()


class BangoPaymentAccountForm(happyforms.Form):
    bankAccountPayeeName = forms.CharField(
        max_length=50, label=_lazy(u'Account Holder Name'))
    companyName = forms.CharField(
        max_length=255, label=_lazy(u'Company Name'))
    vendorName = forms.CharField(
        max_length=255, label=_lazy(u'Vendor Name'))
    financeEmailAddress = forms.EmailField(
        required=True, label=_lazy(u'Financial Email'),
        max_length=100)
    adminEmailAddress = forms.EmailField(
        required=True, label=_lazy(u'Administrative Email'),
        max_length=100)
    supportEmailAddress = forms.EmailField(
        required=True, label=_lazy(u'Support Email'),
        max_length=100)

    address1 = forms.CharField(
        max_length=255, label=_lazy(u'Address'))
    address2 = forms.CharField(
        max_length=255, required=False, label=_lazy(u'Address 2'))
    addressCity = forms.CharField(
        max_length=128, label=_lazy(u'City/Municipality'))
    addressState = forms.CharField(
        max_length=64, label=_lazy(u'State/Province/Region'))
    addressZipCode = forms.CharField(
        max_length=10, label=_lazy(u'Zip/Postal Code'))
    addressPhone = forms.CharField(
        max_length=20, label=_lazy(u'Phone'))
    countryIso = forms.ChoiceField(
        choices=BANGO_COUNTRIES, label=_lazy(u'Country'))
    currencyIso = forms.ChoiceField(
        choices=BANGO_OUTPAYMENT_CURRENCIES,
        label=_lazy(u'I prefer to be paid in'))

    vatNumber = forms.CharField(
        max_length=17, required=False, label=_lazy(u'VAT Number'))

    bankAccountNumber = forms.CharField(
        max_length=20, label=_lazy(u'Bank Account Number'),
        widget=forms.HiddenInput())
    bankAccountCode = forms.CharField(
        max_length=20, label=_lazy(u'Bank Account Code'))
    bankName = forms.CharField(
        max_length=50, label=_lazy(u'Bank Name'))
    bankAddress1 = forms.CharField(
        max_length=50, label=_lazy(u'Bank Address'))
    bankAddress2 = forms.CharField(
        max_length=50, required=False, label=_lazy(u'Bank Address 2'))
    bankAddressCity = forms.CharField(
        max_length=50, required=False, label=_lazy(u'Bank City/Municipality'))
    bankAddressState = forms.CharField(
        max_length=50, required=False,
        label=_lazy(u'Bank State/Province/Region'))
    bankAddressZipCode = forms.CharField(
        max_length=10, label=_lazy(u'Bank Zip/Postal Code'))
    bankAddressIso = forms.ChoiceField(
        choices=BANGO_COUNTRIES, label=_lazy(u'Bank Country'))

    account_name = forms.CharField(max_length=64, label=_lazy(u'Account Name'))

    # These are the fields that Bango uses for bank details. They're read-only
    # once written.
    read_only_fields = set(['bankAccountPayeeName', 'bankAccountNumber',
                            'bankAccountCode', 'bankName', 'bankAddress1',
                            'bankAddressZipCode', 'bankAddressIso',
                            'adminEmailAddress', 'currencyIso'])

    def __init__(self, *args, **kwargs):
        self.account = kwargs.pop('account', None)
        super(BangoPaymentAccountForm, self).__init__(*args, **kwargs)
        if self.account:
            # We don't need the bank account fields if we're getting
            # modifications.
            for field in self.fields:
                if field in self.read_only_fields:
                    self.fields[field].required = False

    def save(self):
        # Save the account name, if it was updated.
        self.account.update_account_details(**self.cleaned_data)


class BangoAccountListForm(happyforms.Form):
    accounts = forms.ModelChoiceField(
        queryset=PaymentAccount.objects.none(),
        label=_lazy(u'Payment Account'), required=False)

    def __init__(self, *args, **kwargs):
        self.addon = kwargs.pop('addon')
        user = kwargs.pop('user')

        super(BangoAccountListForm, self).__init__(*args, **kwargs)

        self.fields['accounts'].queryset = PaymentAccount.objects.filter(
            user=user, inactive=False, agreed_tos=True)

        try:
            current_account = AddonPaymentAccount.objects.get(addon=self.addon)
            self.initial['accounts'] = (
                PaymentAccount.objects.get(uri=current_account.account_uri))
            self.fields['accounts'].empty_label = None
        except (AddonPaymentAccount.DoesNotExist, PaymentAccount.DoesNotExist):
            pass

    def clean_accounts(self):
        if (AddonPaymentAccount.objects.filter(addon=self.addon).exists() and
            not self.cleaned_data.get('accounts')):

            raise forms.ValidationError(
                _('You cannot remove a payment account from an app.'))

        return self.cleaned_data.get('accounts')

    def save(self):
        if self.cleaned_data.get('accounts'):
            try:
                log.info('[1@%s] Deleting app payment account' % self.addon.pk)
                AddonPaymentAccount.objects.get(addon=self.addon).delete()
            except AddonPaymentAccount.DoesNotExist:
                pass

            log.info('[1@%s] Creating new app payment account' % self.addon.pk)
            AddonPaymentAccount.create(
                provider='bango', addon=self.addon,
                payment_account=self.cleaned_data['accounts'])

            # If the app is marked as paid and the information is complete
            # and the app is currently marked as incomplete, put it into the
            # re-review queue.
            if (self.addon.status == amo.STATUS_NULL and
                self.addon.highest_status in amo.WEBAPPS_APPROVED_STATUSES):
                # FIXME: This might cause noise in the future if bank accounts
                # get manually closed by Bango and we mark apps as STATUS_NULL
                # until a new account is selected. That will trigger a
                # re-review.

                log.info(u'[Webapp:%s] (Re-review) Public app, premium type '
                         u'upgraded.' % self.addon)
                RereviewQueue.flag(
                    self.addon, amo.LOG.REREVIEW_PREMIUM_TYPE_UPGRADE)

            _restore_app(self.addon)
