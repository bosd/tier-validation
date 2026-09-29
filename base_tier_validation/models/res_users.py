# Copyright 2019 ForgeFlow S.L. (https://www.forgeflow.com)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models, modules
from odoo.fields import Domain


class Users(models.Model):
    _inherit = "res.users"

    review_ids = fields.Many2many(
        string="Reviews", comodel_name="tier.review", copy=False
    )

    def _review_user_count_groups(self):
        """Systray review groups for ``self`` (one user), per model.

        This is the authoritative absolute count the systray badge shows: the
        records the user can review now, grouped by model. It reads the
        *stored* ``can_review`` (kept correct by ``tier.review``'s sibling
        recompute) and does **not** call ``_update_review_status`` -- so it is a
        handful of cheap, indexed reads and is safe to compute for every
        affected reviewer on each validation (see ``_update_counter``), which is
        what lets the counter update over the bus without an RPC recount.
        """
        self.ensure_one()
        user = self
        user_reviews = {}
        domain = (
            Domain("status", "=", "pending")
            & Domain("can_review", "=", True)
            & Domain("id", "in", user.review_ids.ids)
        )
        review_groups = (
            self.env["tier.review"]
            .sudo()
            ._read_group(
                domain=domain,
                groupby=["model"],
                aggregates=["id:recordset"],
            )
        )
        for model, tier_review in review_groups:
            Model = self.env[model]
            # Skip Models not having Tier Validation enabled (example: was unistalled)
            if tier_review and hasattr(Model, "can_review"):
                records_domain = (
                    Domain("id", "in", tier_review.mapped("res_id"))
                    & Domain("validation_status", "!=", "rejected")
                    & Domain("can_review", "=", True)
                )
                records = (
                    Model.with_user(user)
                    .with_context(active_test=False)
                    .search(records_domain)
                )
                # Excludes any cancelled records depending on the structure of the model
                if Model._state_field in Model._fields:
                    records = records.filtered(
                        lambda x: x[x._state_field] != x._cancel_state
                    )
                if records:
                    user_reviews[model] = {
                        "id": records[0].id,
                        "name": Model._description,
                        "model": model,
                        "active_field": "active" in Model._fields,
                        "icon": modules.module.get_module_icon(Model._original_module),
                        "type": "tier_review",
                        "pending_count": len(records),
                    }
        return list(user_reviews.values())

    @api.model
    def review_user_count(self):
        """The current user's systray review groups (RPC, used on page load).

        Promotes any due reviews first (``_update_review_status``) so a freshly
        loaded page is correct even if a promotion was missed, then delegates to
        the shared, cheap ``_review_user_count_groups``.
        """
        self.env.user.review_ids._update_review_status()
        return self.env.user._review_user_count_groups()
