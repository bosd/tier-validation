# Copyright 2017 ForgeFlow S.L. (https://www.forgeflow.com)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import logging

from odoo import api, fields, models
from odoo.exceptions import AccessError
from odoo.fields import Domain

_logger = logging.getLogger(__name__)

# Reviews reminded per definition per call. The cron reports what is left
# so the worker comes back for the rest instead of doing it all at once.
REMINDERS_PER_DEFINITION = 50


class TierDefinition(models.Model):
    _name = "tier.definition"
    _description = "Tier Definition"

    @api.model
    def _get_default_name(self):
        return self.env._("New Tier Validation")

    @api.model
    def _get_tier_validation_model_names(self):
        res = []
        return res

    name = fields.Char(
        string="Description",
        required=True,
        default=lambda self: self._get_default_name(),
        translate=True,
    )
    model_id = fields.Many2one(
        comodel_name="ir.model",
        string="Referenced Model",
        domain=lambda self: [("model", "in", self._get_tier_validation_model_names())],
    )
    model = fields.Char(related="model_id.model", index=True, store=True)
    review_type = fields.Selection(
        string="Validated by",
        default="individual",
        selection=[
            ("individual", "Specific user"),
            ("group", "Any user in a specific group"),
            ("field", "Field in related record"),
        ],
    )
    allow_write_for_reviewer = fields.Boolean(
        string="Allow Write For Reviewers",
        default=False,
    )
    reviewer_id = fields.Many2one(comodel_name="res.users")
    reviewer_group_id = fields.Many2one(
        comodel_name="res.groups", string="Reviewer group"
    )
    reviewer_field_id = fields.Many2one(
        comodel_name="ir.model.fields",
        string="Reviewer field",
        domain="[('id', 'in', valid_reviewer_field_ids)]",
    )
    valid_reviewer_field_ids = fields.One2many(
        comodel_name="ir.model.fields",
        compute="_compute_domain_reviewer_field",
    )
    definition_type = fields.Selection(
        string="Definition", selection=[("domain", "Domain")], default="domain"
    )
    definition_domain = fields.Char()
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=30)
    company_id = fields.Many2one(
        comodel_name="res.company",
        default=lambda self: self.env.company,
    )
    notify_on_create = fields.Boolean(
        string="Notify Reviewers on Creation",
        help="If set, all possible reviewers will be notified by email when "
        "this definition is triggered.",
    )
    notify_on_pending = fields.Boolean(
        string="Notify Reviewers on reaching Pending",
        help="If set, all possible reviewers will be notified by email when "
        "this status is reached."
        "Usefull in an Approve by sequence scenario. "
        "An notification request to review is sent out when it's their turn to review.",
    )
    notify_on_accepted = fields.Boolean(
        string="Notify Reviewers on Accepted",
        help="If set, reviewers will be notified by email when a review related "
        "to this definition is accepted.",
    )
    notify_on_rejected = fields.Boolean(
        string="Notify Reviewers on Rejected",
        help="If set, reviewers will be notified by email when a review related "
        "to this definition is rejected.",
    )
    notify_on_restarted = fields.Boolean(
        string="Notify Reviewers on Restarted",
        help="If set, reviewers will be notified by email when a reviews related "
        "to this definition are restarted.",
    )
    has_comment = fields.Boolean(string="Comment", default=False)
    comment_approve_default = fields.Char(
        string="Approve Comment",
        help="Default comment prefilled when approval comments are enabled.",
    )
    notify_reminder_delay = fields.Integer(
        string="Remind Every (Days)",
        help="Remind the reviewers every this many days while their review is "
        "pending. Leave at 0 to send no reminder.",
    )
    notify_reminder_type = fields.Selection(
        selection=[("activity", "Activity"), ("message", "Message")],
        string="Remind by",
        default="activity",
        required=True,
        help="Activity: one to-do per reviewer on the document, refreshed "
        "until the review is done. Reviews with several reviewers are "
        "reminded by message.\n"
        "Message: a message on the document, without adding to the "
        "reviewer's activities.",
    )
    approve_sequence = fields.Boolean(
        string="Approve by sequence",
        default=False,
        help="Approval order by the specified sequence number",
    )
    approve_sequence_bypass = fields.Boolean(
        help="Bypassed (auto validated), if previous tier was validated "
        "by same reviewer",
    )
    exclude_requester = fields.Boolean(
        string="Four-eyes Principle",
        default=False,
        help="When set, the user who requested the validation is removed "
        "from this tier's reviewer pool. Anyone else in the configured "
        "group can validate -- except the requester themselves. Without "
        "this flag, a requester who happens to be in the same group as "
        "the configured reviewers can auto-validate their own request. "
        "Only meaningful for review_type='group'; the form hides this "
        "field for the other review types.",
    )

    @api.onchange("review_type")
    def onchange_review_type(self):
        self.reviewer_id = None
        self.reviewer_group_id = None

    def _reviewers_without_model_access(self, model_name, users):
        """Return the subset of ``users`` that cannot read ``model_name``.

        The check is model-level only (``ir.model.access``). It does not
        evaluate per-record ``ir.rule`` restrictions, so callers should
        treat a non-empty result as "*probably* no access" rather than a
        guarantee -- record rules can grant or revoke access dynamically
        at runtime.
        """
        Model = self.env.get(model_name)
        if Model is None:
            return self.env["res.users"]
        no_access = self.env["res.users"]
        for user in users:
            try:
                Model.with_user(user).check_access("read")
            except AccessError:
                no_access |= user
        return no_access

    def _reviewers_to_check_for_access(self):
        """Return the user recordset whose model access we can check ahead
        of time for this definition. Skip review types where the reviewer
        only resolves at validation time (``field`` reads off the record)."""
        self.ensure_one()
        if self.review_type == "individual":
            return self.reviewer_id
        if self.review_type == "group":
            return self.reviewer_group_id.user_ids
        return self.env["res.users"]

    @api.onchange("review_type", "reviewer_id", "reviewer_group_id", "model_id")
    def _onchange_warn_reviewer_access(self):
        """Advisory warning when an assigned reviewer cannot read the model.

        Non-blocking: legitimate workflows (admin is about to grant the
        group, ir.rules expose specific records, ...) still save. The
        warning just makes it impossible to misconfigure this silently.
        """
        # Read the model name off the m2o directly rather than via the
        # stored related ``self.model`` -- on ``.new()`` records the
        # related can still be empty depending on cache state.
        model_name = self.model_id.model
        if not (model_name and self.review_type):
            return
        users = self._reviewers_to_check_for_access()
        if not users:
            return
        no_access = self._reviewers_without_model_access(model_name, users)
        if not no_access:
            return
        return {
            "warning": {
                "title": self.env._("Reviewer may lack access"),
                "message": self.env._(
                    "The following reviewer(s) may not be able to read "
                    "'%(model)s' records and so cannot act on the reviews "
                    "this tier will create: %(reviewers)s.\n\n"
                    "This is a best-effort check against model-level access "
                    "rights only -- record rules may still grant or revoke "
                    "access at runtime. Make sure these users belong to a "
                    "group with read access on the target model, or pick "
                    "different reviewers.",
                    model=self.model_id.name or model_name,
                    reviewers=", ".join(no_access.mapped("display_name")),
                ),
            }
        }

    @api.depends("review_type", "model_id")
    def _compute_domain_reviewer_field(self):
        models = self.mapped("model")
        IrModelFields = self.env["ir.model.fields"].sudo()
        valid_reviewer_fields = dict(
            IrModelFields._read_group(
                domain=Domain("model", "in", models)
                & (
                    Domain("relation", "=", "res.users")
                    | Domain("relation", "=", "res.groups")
                ),
                groupby=["model"],
                aggregates=["id:array_agg"],
            )
        )
        for rec in self:
            rec.valid_reviewer_field_ids = valid_reviewer_fields.get(
                rec.model, IrModelFields
            )

    def _reviews_needing_reminder_domain(self):
        """Domain of the reviews of this definition that are due a reminder,
        or ``None`` when it has no reminder set up."""
        self.ensure_one()
        if not self.notify_reminder_delay:
            return None
        review_date = fields.Datetime.subtract(
            fields.Datetime.now(), days=self.notify_reminder_delay
        )
        return (
            Domain("definition_id", "=", self.id)
            # A waiting review is not the reviewer's turn yet: nothing to remind.
            & Domain("status", "=", "pending")
            & (
                Domain("create_date", "<", review_date)
                & Domain("last_reminder_date", "=", False)
                | Domain("last_reminder_date", "<", review_date)
            )
        )

    def _count_reviews_needing_reminder(self):
        """How many reviews are due a reminder, behind the capped page that
        _get_review_needing_reminder returns. The cron reports this as the
        remaining count, which is what makes the worker come back for the
        rest of a backlog within the same run."""
        domain = self._reviews_needing_reminder_domain()
        if domain is None:
            return 0
        return self.env["tier.review"].search_count(domain)

    def _get_review_needing_reminder(self):
        """Return all the reviews that have the reminder setup."""
        domain = self._reviews_needing_reminder_domain()
        if domain is None:
            return self.env["tier.review"]
        return self.env["tier.review"].search(
            domain,
            limit=REMINDERS_PER_DEFINITION,
        )

    def _cron_send_review_reminder(self):
        """Remind the reviewers of the reviews that are due, a definition at
        a time.

        Since 19.0 a cron reports its progress and is rescheduled when it runs
        out of time, instead of being expected to finish in one go. Doing the
        whole backlog in a single transaction is what makes this cron fail on
        a busy database: the worker kills the run on its time limit, the whole
        transaction is rolled back, and every reminder that had already been
        sent is due again on the next run. The backlog then only grows, every
        following run is killed as well, and Odoo deactivates the cron after
        enough consecutive failures.

        So commit per definition and stop while there is still time left. The
        remaining count is in reviews, not definitions, because each
        definition only hands out REMINDERS_PER_DEFINITION at a time: as long
        as reviews are left the worker calls this again inside the same run,
        and when the time runs out the job is rescheduled ASAP with what is
        done kept.
        """
        cron = self.env["ir.cron"]
        definition_with_reminder = self.env["tier.definition"].search(
            Domain("notify_reminder_delay", ">", 0)
        )
        remaining = sum(
            record._count_reviews_needing_reminder()
            for record in definition_with_reminder
        )
        time_left = cron._commit_progress(remaining=remaining)
        for record in definition_with_reminder:
            if not time_left:
                break
            # Read before the try: after a database error the cursor is
            # aborted and any field read that is not already cached would
            # raise again from inside the except.
            label = (record.id, record.display_name)
            try:
                review_to_remind = record._get_review_needing_reminder()
                processed = len(review_to_remind)
                if review_to_remind:
                    review_to_remind._send_review_reminder()
            except Exception:  # noqa: BLE001
                # One definition must not cost the whole run. Roll back first,
                # so logging cannot hit an aborted cursor.
                self.env.cr.rollback()
                _logger.exception(
                    "Could not send the reminders of tier definition %s (%s)",
                    *label,
                )
                # Give up on this definition for this run rather than letting
                # the worker come back to it until the time budget is gone.
                # Its reviews keep their last_reminder_date, so they are due
                # again on the next run; the traceback above is the signal.
                remaining = max(remaining - record._count_reviews_needing_reminder(), 0)
                processed = 0
            else:
                remaining = max(remaining - processed, 0)
            time_left = cron._commit_progress(processed, remaining=remaining)
