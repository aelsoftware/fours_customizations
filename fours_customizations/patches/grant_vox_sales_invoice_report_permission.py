"""Move Vox's Sales Invoice report grant out of the shared Kit POS app."""


def execute():
	from fours_customizations.vox_kit_pos import ensure_vox_kit_pos_permissions

	return ensure_vox_kit_pos_permissions()
