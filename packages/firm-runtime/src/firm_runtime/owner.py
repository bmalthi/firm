"""Owner-only configuration repair and explicit role stop/routing operations."""

import json

from . import analysis_history, github
from .errors import FirmError
from .lifecycle import _require_address, validate_config
from .store import OWNER, encoded


class OwnerControls:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store

    @staticmethod
    def _require_owner(actor):
        if actor != OWNER:
            raise FirmError("only the owner may change execution routing or configuration")

    def configure(self, actor, config):
        self._require_owner(actor)
        config = validate_config(config)
        # Reject local state conflicts before an optional GitHub setup call can create
        # or update anything outside the firm database. Recheck after that call so the
        # eventual write is still based on current role state.
        with self.store.read() as db:
            self.validate_reconfiguration(db, config)
        app = github.connect(config["github"]) if "github" in config else None
        with self.store.transaction() as db:
            self.validate_reconfiguration(db, config)
            corrected = self.runtime.context.refresh_policies(db, config)
            db.execute("UPDATE firm SET config_json=?", (encoded(config),))
            github.record_app(db, app)
        return {
            "status": "applied",
            "retained_sessions": (
                "keep captured execution identity; matching context policy updated"
            ),
            "retained_context_policies_updated": corrected,
            "shared_holds": "require explicit incident clearance after repair",
        }

    def validate_reconfiguration(self, db, config):
        used = {
            row[0]
            for row in db.execute(
                "SELECT execution_profile FROM roles WHERE employment!='retired'"
            )
        }
        if used - config["profiles"].keys():
            raise FirmError("configuration must retain current roles' primary profiles")
        if self.runtime.accounts.enabled(db) and "accounts" not in config:
            raise FirmError("cannot remove named accounts from an execution host")
        for row in db.execute(
            "SELECT DISTINCT account_id,provider FROM summary_runs "
            "WHERE kind='task' AND state='running'"
        ):
            account = config.get("accounts", {}).get(row["account_id"])
            if account is None or account["provider"] != row["provider"]:
                raise FirmError("configuration must retain accounts held by tasks")

    @staticmethod
    def _scope(address):
        """A role address and its subtree, or every role of the firm."""
        if address is None:
            return ""
        _require_address(address)
        return address

    def revoke_github(self, actor, repo, address=None):
        """Refuse the repository to an address, or every role, until the owner grants it again."""
        self._require_owner(actor)
        return self.runtime.github.revoke(repo, self._scope(address))

    def grant_github(self, actor, repo, address=None):
        """Clear an address's own revocation and grant it write, or every revocation without one.

        A wider revocation covering the address is refused rather than lifted for everyone.
        """
        self._require_owner(actor)
        return self.runtime.github.allow(repo, self._scope(address))

    def stop_role(self, actor, employee_id, request_id):
        self._require_owner(actor)
        with self.store.transaction() as db:
            role = self.store.role(db, employee_id)

            def stop():
                if role["employment"] == "retired":
                    raise FirmError("retired roles cannot be stopped or reopened")
                db.execute(
                    "UPDATE roles SET employment='paused' WHERE employee_id=?", (employee_id,)
                )
                active = [
                    r[0]
                    for r in db.execute(
                        "SELECT activation_id FROM activations WHERE employee_id=? "
                        "AND state IN ('admitted','running')",
                        (employee_id,),
                    )
                ]
                for aid in active:
                    db.execute(
                        "UPDATE activations SET token_hash=NULL WHERE activation_id=?", (aid,)
                    )
                    db.execute(
                        "UPDATE sandbox_runs SET stop_cause='owner_stop' WHERE activation_id=?",
                        (aid,),
                    )
                analysis_history.role_changed(db, self.store.clock(), role, employee_id)
                return {
                    "employee_id": employee_id,
                    "status": "stopping" if active else "paused",
                    "execution_exit_confirmed": not bool(active),
                }

            return self.store.request(
                db, actor, request_id, "stop_role", {"employee_id": employee_id}, stop
            )

    def route(self, actor, employee_id, profile, request_id):
        """Explicit fresh start; callers first stop live execution and wait for confirmation."""
        self._require_owner(actor)
        with self.store.transaction() as db:
            role = self.store.role(db, employee_id)

            def change():
                if role["employment"] == "retired":
                    raise FirmError("retired roles cannot be routed")
                if db.execute(
                    "SELECT 1 FROM activations WHERE employee_id=? "
                    "AND state IN ('admitted','running')",
                    (employee_id,),
                ).fetchone():
                    raise FirmError(
                        "confirm the old execution has stopped before routing fresh work"
                    )
                if profile not in self.runtime.accounts.config(db)["profiles"]:
                    raise FirmError("unknown execution profile")
                recovery = json.loads(role["recovery_json"]) if role["recovery_json"] else None
                if recovery:
                    recovery["context_retained"] = False
                db.execute(
                    "UPDATE roles SET execution_profile=?,effort=NULL,session_id=NULL,"
                    "employment='active',recovery_json=? "
                    "WHERE employee_id=?",
                    (profile, encoded(recovery) if recovery else None, employee_id),
                )
                self.runtime.levels.mark_routed(db, employee_id)
                db.execute("DELETE FROM role_dependencies WHERE employee_id=?", (employee_id,))
                self.store.request_execution(db, employee_id, "owner_fresh_route")
                analysis_history.role_changed(db, self.store.clock(), role, employee_id)
                if role["execution_profile"] == profile:
                    analysis_history.record(
                        db,
                        self.store.clock(),
                        "role.routed",
                        employee_id,
                        profile=profile,
                        fresh=True,
                    )
                return {
                    "employee_id": employee_id,
                    "profile": profile,
                    "status": "queued",
                    "context": "fresh",
                    "attempt_budget_preserved": True,
                }

            return self.store.request(
                db,
                actor,
                request_id,
                "route_fresh",
                {"employee_id": employee_id, "profile": profile},
                change,
            )

    def resume_role(self, actor, employee_id, request_id):
        self._require_owner(actor)
        with self.store.transaction() as db:
            role = self.store.role(db, employee_id)

            def resume():
                if role["employment"] == "retired":
                    raise FirmError("retired roles cannot be resumed")
                if role["employment"] != "paused":
                    raise FirmError("only a stopped role can be resumed")
                if db.execute(
                    "SELECT 1 FROM activations WHERE employee_id=? "
                    "AND state IN ('admitted','running')",
                    (employee_id,),
                ).fetchone():
                    raise FirmError("wait for the old execution to exit before resuming")
                db.execute(
                    "UPDATE roles SET employment='active' WHERE employee_id=?", (employee_id,)
                )
                self.store.request_execution(db, employee_id, "owner_resume")
                analysis_history.role_changed(db, self.store.clock(), role, employee_id)
                return {"employee_id": employee_id, "status": "queued", "holds_preserved": True}

            return self.store.request(
                db, actor, request_id, "resume_role", {"employee_id": employee_id}, resume
            )
