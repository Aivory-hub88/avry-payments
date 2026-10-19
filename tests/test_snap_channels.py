"""Which channels the checkout may ask Snap to show."""
from app.services.payment_gateway import MidtransPaymentService as G


def test_virtual_account_tile_expands_to_every_bank():
    assert G.normalise_channels(["bank_transfer"]) == [
        "bni_va", "bri_va", "echannel", "permata_va", "cimb_va", "bsi_va", "other_va",
    ]


def test_card_wallets_and_qris_still_pass_through():
    assert G.normalise_channels(["Debit Card", "gopay", "qris"]) == ["credit_card", "gopay", "qris"]


def test_unknown_or_unactivated_names_are_dropped():
    assert G.normalise_channels(["bca_va", "indomaret", "akulaku"]) == []


def test_default_offers_cards_wallets_qris_and_vas():
    assert set(G.DEFAULT_SNAP_CHANNELS) >= {"credit_card", "gopay", "qris", "bni_va", "echannel", "bsi_va"}
