"""An environment's size, life and cost: the VM a plan rents, the node's limits (machines at once, the
longest life, the month's budget) and what the month has cost so far. A mixin of EnvironmentManager."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from openajar.core.errors import InvalidRequest, NotFound, OpenajarError
from openajar.environments import machine
from openajar.environments.errors import (
    EnvironmentFull,
    EnvironmentLimit,
)
from openajar.environments.models import (
    SIZES,
    WORKSPACE,
    Environment,
)

ROOM_SHOWN = 2  # sizes a capacity error offers (the error must fit in an MCP error with them)
NO_ROOM_FRESH = timedelta(minutes=30)  # a machine's open that had no room for some types is news this long


class Sizing:
    """Sizes, hours and money, for EnvironmentManager (it reads self.settings, self.store and self.vms())."""

    def _size(self, size: str | None) -> tuple[str | None, dict | None]:
        """`size`: small, medium, large or xlarge (SIZES: the cheapest server type with at least
        that much, or on the docker provider a container with those limits), a vms.toml profile
        name, or a server type. None: the environments' profile, else the VM default (the cheapest
        that fits)."""
        if not size:
            return self.settings.profile, None
        size = str(size).strip().lower()
        vms = self.vms()
        if size in SIZES:
            self._check_size(size)
            named = getattr(vms.provider(), "size_type", None)  # containers (docker, a person's own machine)
            if callable(named):
                return self.settings.profile, {"server_type": named(size)}
            cpu, memory = SIZES[size]
            return self.settings.profile, {"min_cpu": cpu, "min_memory_gb": memory}
        if size in vms.settings.profiles:
            return size, None
        return self.settings.profile, {"server_type": size}

    def _check_size(self, size: str) -> None:
        names = list(SIZES)
        top = self.settings.max_size
        if names.index(size) > names.index(top):
            raise EnvironmentLimit(f"the biggest environment on this node is {top} ({self._size_words(top)}), so "
                                   f"{size} is past the limit. Ask for {top} or less, or tell the user: the owner "
                                   f"raises it with `openajar environments allow --max-size {size}`")

    def _size_names(self) -> str:
        """small, medium, large or xlarge: the sizes an agent may ask for here (up to max_size)."""
        names = list(SIZES)[:list(SIZES).index(self.settings.max_size) + 1]
        return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"

    async def _check_server_type(self, name: str) -> None:
        """A size that is no size and no profile must be a server type the provider rents, checked
        before anything is planned: an unknown one (a typo like "xxlarge") is invalid, not a VM error.
        When the provider's list cannot be read, the plan itself says what is wrong."""
        try:
            types = [str(t.get("name")) for t in await self.vms().provider().server_types()]
        except OpenajarError:
            return
        if types and name not in types:
            example = next((t for t in types if not t.startswith(("docker", "own"))), types[0])
            raise InvalidRequest(f"size {name!r} is not one this node knows. size is one of {self._size_names()}, "
                                 f"or a server type the provider rents, like {example}; leave it out for the "
                                 "default. Nothing was rented")

    @staticmethod
    def _size_words(size: str) -> str:
        cpu, memory = SIZES[size]
        return f"{cpu} CPU, {memory} GB"

    def _check_planned(self, plan: dict) -> None:
        """A server type or a profile asked for by name is bound by max_size too, by what it has."""
        specs = plan.get("specs") or {}
        top = self.settings.max_size
        cpu, memory = SIZES[top]
        if (specs.get("cores") or 0) > cpu or (specs.get("memory_gb") or 0) > memory:
            need = next((n for n, (c, m) in SIZES.items()
                         if c >= (specs.get("cores") or 0) and m >= (specs.get("memory_gb") or 0) - 0.5), None)
            more = (f"or tell the user: the owner raises the limit with `openajar environments allow --max-size {need}`"
                    if need else f"no environment may be bigger than {list(SIZES)[-1]}")
            raise EnvironmentLimit(f"{plan['server_type']} has {specs.get('cores')} CPU and {specs.get('memory_gb'):g} "
                                   f"GB, more than this node's biggest environment, {top} ({self._size_words(top)}). "
                                   f"Ask for {top} or a smaller server type; {more}. Nothing was rented")

    @staticmethod
    def size_of(cpu: float | None, memory_gb: float | None) -> str | None:
        """The biggest of SIZES a machine with this much has (None: less than small, or unknown)."""
        got = None
        for name, (c, m) in SIZES.items():
            if (cpu or 0) >= c and (memory_gb or 0) >= m - 0.5:  # a few hundred MB short still counts
                got = name
        return got

    def _vm_of(self, env: Environment) -> Any:
        """The VM record of `env`'s machine, None when it has none or it cannot be read."""
        with contextlib.suppress(NotFound, OpenajarError):
            return self.vms().store.get(env.vm_id) if env.vm_id else None
        return None

    def _no_room_types(self, env: Environment) -> set[str]:
        """The server types that had no room when `env`'s machine was made, while that is recent."""
        rec = self._vm_of(env)
        if rec is None or not rec.no_room or self._clock() - rec.created_at > NO_ROOM_FRESH:
            return set()
        return {t for t, _loc in rec.no_room}

    def machine_notes(self, env: Environment) -> dict:
        """{"fell_back": words} when the planned type had no room and the machine is of another type
        (another family, maybe dearer): what it costs, so the agent and the owner see it. Else {}."""
        rec = self._vm_of(env)
        if rec is None or not rec.asked:
            return {}
        from openajar.vms.capacity import fell_back_words

        return {"fell_back": fell_back_words(rec.asked, rec.server_type, rec.price_hourly_eur, rec.no_room)}

    async def bigger_sizes(self, env: Environment) -> list[dict]:
        """The sizes above the one `env` has, up to max_size, with what each would rent here and its
        price: what an agent reads before it asks for a bigger one. A type that just had no room for
        this machine is passed over for the size's next one, and a size with none left is left out. An
        entry that costs less an hour than this machine says so ("cheaper" and a "note"). [] when none,
        or the provider does not answer."""
        names = list(SIZES)
        top = names.index(self.settings.max_size)
        got = env.machine.size
        start = names.index(got) + 1 if got in names else 0
        no_room, running = self._no_room_types(env), env.price_hourly_eur
        fell_back = bool(getattr(self._vm_of(env), "asked", None))
        out = []
        for name in names[start:top + 1]:
            cpu, memory = SIZES[name]
            if (env.machine.cpu or 0) >= cpu and (env.machine.memory_gb or 0) >= memory:
                continue
            try:
                profile, custom = self._size(name)
                plan = await self.vms().plan(f"{WORKSPACE}/{env.id}", profile=profile, ttl_minutes=60, size=custom,
                                             image=env.image or None)
                picks = [(plan["server_type"], plan.get("price_hourly_eur")), *(plan.get("fallbacks") or [])]
                server_type, price = next(((t, p) for t, p in picks if t not in no_room), (None, None))
                if server_type is None:
                    continue  # every type of this size just had no room
                specs = (plan.get("specs") or {}) if server_type == plan["server_type"] else next(
                    (t for t in await self.vms().offers(plan["location"]) if t["name"] == server_type), {})
            except OpenajarError:
                continue
            row = {"size": name, "server_type": server_type, "cpu": specs.get("cores"),
                   "memory_gb": specs.get("memory_gb"), "price_hourly_eur": price}
            if running and price is not None and price < running:
                row["cheaper"] = True
                row["note"] = (f"costs less an hour than this machine (EUR {running:.4f}), which is "
                               + ("a dearer type rented because the cheaper ones had no room" if fell_back
                                  else "a dearer type") + f"; {server_type} may have no room either")
            out.append(row)
        return out

    async def sizes_with_room(self, env: Environment, error: BaseException) -> list[dict]:
        """The sizes next to the one `env` asked for (up to max_size, nearest first, at most ROOM_SHOWN)
        that may have room: what a capacity error offers instead. A size is never offered when every
        server type it would try just had no room (NoRoom.tried, in any location). When the provider
        lists what is orderable now, a size must have a type it lists in the zone ("now": true); when it
        does not say, a size with a type not tried yet is offered as that ("now": false). Never taken for
        the agent: it asks for one itself."""
        names = list(SIZES)[:list(SIZES).index(self.settings.max_size) + 1]
        asked = env.machine.size if env.machine.size in SIZES else None
        at = list(SIZES).index(asked) if asked else 0
        failed = set(getattr(error, "tried", None) or [])
        failed_types = {t for t, _loc in failed}
        says: bool | None = None  # does the provider list what is orderable now (its types' `available`)
        out: list[dict] = []
        for name in sorted((n for n in names if n != asked), key=lambda n: abs(list(SIZES).index(n) - at)):
            try:
                profile, custom = self._size(name)
                plan = await self.vms().plan(f"{WORKSPACE}/{env.id}", profile=profile, ttl_minutes=60, size=custom,
                                             image=env.image or None)
                types = [plan["server_type"], *[t for t, _p in plan.get("fallbacks") or []]]
                if all(t in failed_types for t in types):
                    continue  # every type it would try just had no room
                if says is None:
                    says = any(t.get("available") is not None for t in await self.vms().offers(plan["location"]))
                if says and not await self.vms().listed_now(types, plan["location"], skip=failed):
                    continue
            except OpenajarError:
                continue
            cpu, memory = SIZES[name]
            out.append({"size": name, "cpu": cpu, "memory_gb": memory, "now": bool(says),
                        "smaller": asked is not None and list(SIZES).index(name) < at})
            if len(out) >= ROOM_SHOWN:
                break
        return out

    def _hours(self, hours: float | None) -> int:
        h = float(hours if hours is not None else self.settings.default_hours)
        if h <= 0:
            raise InvalidRequest("hours is how long the environment may live, more than 0")
        cap = self._longest()
        minutes = max(5, round(h * 60))
        if minutes > cap:
            raise EnvironmentLimit(f"an environment lives at most {cap / 60:g} hours on this node, so {h:g} hours "
                                   f"is past the limit. Ask for fewer hours; {self._longest_raise()}")
        return minutes

    def _longest(self) -> float:
        """The longest life of an environment here, in minutes: max_hours, and the VMs' max_ttl_minutes."""
        return min(self.settings.max_hours * 60, self.vms().settings.max_ttl_minutes)

    def _longest_raise(self) -> str:
        """Which setting holds the longest life, and how the owner raises it."""
        if self.settings.max_hours * 60 <= self.vms().settings.max_ttl_minutes:
            return "the owner raises it with `openajar environments allow --max-hours H`"
        return "the owner raises it with max_ttl_minutes in vms.toml (the longest life of any VM on this node)"

    async def _image(self) -> str:
        if self.settings.image != "auto":
            return self.settings.image
        try:
            await self.vms().provider().resolve_image(machine.SNAPSHOT_IMAGE)
            return machine.SNAPSHOT_IMAGE
        except OpenajarError:
            return machine.BASE_IMAGE

    def month_spend(self, now: datetime | None = None) -> float:
        now = now or self._clock()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = 0.0
        for env in self.store.recent(1000):
            if env.closed_at is not None and env.closed_at < start:
                continue
            total += self.cost_now(env) or 0.0
        return round(total, 4)

    def booked_eur(self, now: datetime | None = None, *, until: dict[str, datetime] | None = None) -> float:
        """This month's spend on environments with every open one counted to its time limit (or to
        `until[id]`): what they can still cost if none closes early. The budget is checked against it."""
        now = now or self._clock()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = 0.0
        for env in self.store.recent(1000):
            if env.closed_at is not None and env.closed_at < start:
                continue
            spent = self.cost_now(env) or 0.0
            if env.open and env.price_hourly_eur:
                end = (until or {}).get(env.id, env.expires_at)
                hours = max(1, -(-int(max(0.0, (end - env.created_at).total_seconds())) // 3600))
                spent = max(spent, hours * env.price_hourly_eur)
            total += spent
        total += self.side_booked_eur(now)  # warm machines and prepared machines' builds (openajar.environments.warmpool)
        return round(total, 4)

    def cost_now(self, env: Environment) -> float | None:
        if env.cost_eur is not None and not env.open:
            return env.cost_eur
        if not env.vm_id:
            return 0.0 if not env.open else None
        try:
            return self.vms().store.get(env.vm_id).cost_eur(self._clock())
        except (NotFound, OpenajarError):
            return env.cost_eur

    async def plan(self, env_id: str, *, size: str | None, hours: float | None, keep: str = "none",
                   image: str | None = None) -> dict:
        """What open would rent, for the limits and the record of the open (openajar.vms plan, with
        the image the environment boots). keep "live": LIVE_HOURS at a time (renewed by the reaper),
        past max_hours: the owner allowed kept machines."""
        from openajar.environments.saving import LIVE_HOURS

        profile, custom = self._size(size)
        kept = keep == "live"
        minutes = LIVE_HOURS * 60 if kept and hours is None else (round(float(hours) * 60) if kept else self._hours(hours))
        if custom and "server_type" in custom and size and str(size).strip().lower() not in SIZES:
            await self._check_server_type(custom["server_type"])
        image = image or await self._image()  # planned with it: no fallback type has a smaller disk than it
        from openajar.vms.capacity import NotSold

        try:
            plan = await self.vms().plan(f"{WORKSPACE}/{env_id}", profile=profile, ttl_minutes=minutes, size=custom,
                                         kept=kept, image=image)
        except NotSold as e:  # a server type by name the location does not sell: a size picks one that is sold
            fits = self.size_of(*e.has)
            tip = (f'{", or" if e.words.endswith("one of those") else ". Ask for"} size="{fits}" '
                   f"({self._size_words(fits)}), which picks a type sold there") if fits else ""
            raise InvalidRequest(f"{e.words}{tip}. Nothing was rented") from None
        except OpenajarError as e:
            if e.code != "vm_error":
                raise
            raise type(e)(f"{e.message.rstrip('.')}. Nothing was rented") from None  # only planned, never made
        if size and str(size).strip().lower() not in SIZES:
            self._check_planned(plan)  # a server type or a profile, by what it has (a named size: by its name)
        plan["image"] = image
        plan["keep"] = keep
        return plan

    def count_cap(self) -> int:
        """How many machines may run at once: environments.toml max_live, else vms.toml's. openajar.vms
        holds its own max_live at least this high (VmManager.max_live), so this is the one count."""
        return self.settings.max_live if self.settings.max_live is not None else self.vms().settings.max_live

    def machines(self, *, besides: str | None = None) -> tuple[list[Environment], list[Any]]:
        """What counts against count_cap: the open environments (starting, ready, closing), and the
        node's other VMs that still run (one a closed environment's close still deletes, an image
        build, the owner's own; not a warm machine, which yields). `besides`: an environment not to count (the one being brought up)."""
        open_ = [e for e in self.store.open() if e.id != besides]
        have = {e.vm_id for e in open_ if e.vm_id}
        others = []
        with contextlib.suppress(OpenajarError):
            from openajar.environments.warmpool import is_warm

            others = [r for r in self.vms().store.live()  # a warm machine yields to an open (warmpool.make_room)
                      if r.id not in have and not (besides and r.environment == besides) and not is_warm(r)]
        return open_, others

    def check_count(self, *, waited_s: float | None = None, besides: str | None = None) -> None:
        """environment_full when the node already runs count_cap machines. Its words say how many,
        when the soonest may free (its idle close or its time limit; only its title), and what helps."""
        open_, others = self.machines(besides=besides)
        cap, n = self.count_cap(), len(open_) + len(others)
        if n < cap:
            return
        raise self.full_error(open_, others, cap, waited_s=waited_s)

    def full_error(self, open_: list[Environment], others: list[Any], cap: int, *,
                   waited_s: float | None = None) -> EnvironmentFull:
        """environment_full's words (they must fit an MCP error, 600 characters, with a title of 80)."""
        now = self._clock()
        n = len(open_) + len(others)
        words = f"this node already runs {n} machine{'s' if n != 1 else ''}, its limit at once"
        going = [e for e in open_ if e.state == "closing"] + [r for r in others if r.state == "deleting"]
        if going:
            words += f" ({len(going)} closing now)"
        if waited_s and waited_s >= 1:
            words += f"; none freed in the {waited_s:.0f} s this call waited"
        soon = self._soonest_free(open_, others, now)
        retry = 60
        if soon is not None:
            at, title, why = soon
            secs = max(0, int((at - now).total_seconds()))
            retry = min(600, max(30, secs))
            words += (f". The soonest may free at {at.astimezone(UTC).strftime('%H:%M UTC')} "
                      f"({'in a minute' if secs < 90 else f'in {secs // 60} min'}): "
                      + (f"\"{title}\" {why}" if title else why))
        words += (". Nothing was opened. Wait and call again (environment_open waits up to its wait_s for one), or "
                  "close one of yours you no longer need; never one another chat works in. The owner raises the "
                  "limit with `openajar environments allow --max-live N`")
        return EnvironmentFull(words, retry_after=retry)

    def _soonest_free(self, open_: list[Environment], others: list[Any], now: datetime
                      ) -> tuple[datetime, str | None, str] | None:
        """(when, title, why) of the machine that may free first: one closing or being deleted now,
        else the soonest idle close (keep none or save; one with unsaved work is kept) or time limit."""
        if [e for e in open_ if e.state == "closing"] or [r for r in others if r.state == "deleting"]:
            return now + timedelta(minutes=1), None, "one closing now is deleted"
        best: tuple[datetime, str | None, str] | None = None
        idle = timedelta(minutes=self.settings.idle_minutes)
        for e in open_:
            if e.keep == "live":
                continue  # kept running: it frees only when someone closes it
            picks = [(e.expires_at, "at its time limit")]
            with contextlib.suppress(Exception):
                idle_at = self._last_used(e) + idle
                if idle_at > now:  # past it and still open: kept (unsaved work, or busy), so its time limit counts
                    picks.append((idle_at, f"if it gets no call for {self.settings.idle_minutes} min"))
            at, why = min(picks, key=lambda p: p[0])
            at = max(at, now)
            if best is None or at < best[0]:
                best = (at, e.title, why)
        for r in others:
            if r.expires_at is not None and (best is None or r.expires_at < best[0]):
                best = (max(r.expires_at, now), None, "another VM of the node, at its time limit")
        return best

    def check_limits(self, plan: dict) -> None:
        self.check_count()
        budget = self.settings.monthly_eur
        price = plan.get("price_hourly_eur")
        if budget and price is not None:  # the planned type must fit; a dearer fallback that does not is dropped
            from openajar.vms.capacity import affordable

            hours, booked = -(-plan["ttl_minutes"] // 60), self.booked_eur()
            worst = price * hours
            plan["fallbacks"] = affordable(price, plan.get("fallbacks") or [], hours, budget - booked)
            if booked + worst > budget:
                raise EnvironmentLimit(f"this environment could cost up to EUR {worst:.2f}; with EUR {booked:.2f} "
                                       "spent and booked by open environments this month, that passes the monthly "
                                       f"budget of EUR {budget:.2f}. Ask for fewer hours, close one you opened and "
                                       "no longer need, or tell the user: the owner raises the budget with "
                                       "`openajar environments allow --monthly-eur E`")
