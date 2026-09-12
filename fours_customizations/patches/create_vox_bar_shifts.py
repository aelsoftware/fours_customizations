"""Move Vox's recurring bar shifts out of the shared Kit POS app."""


def execute():
	from fours_customizations.vox_kit_pos import ensure_vox_bar_shifts

	return ensure_vox_bar_shifts()
