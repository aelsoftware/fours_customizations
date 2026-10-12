from frappe.model.document import Document


class VoxKitchenModifierOption(Document):
	def before_validate(self):
		from fours_customizations.vox_modifiers import validate_private_record

		validate_private_record(self)

	def before_insert(self):
		self.before_validate()

	def validate(self):
		self.before_validate()

	def on_trash(self):
		from fours_customizations.vox_modifiers import prevent_private_change

		prevent_private_change(self)

	def before_rename(self, *args, **kwargs):
		from fours_customizations.vox_modifiers import prevent_private_change

		prevent_private_change(self)
