"""
MapleStory Idle Bot - Automated party quest runner.
Smart detection based on available templates.
"""
import random
import time
import sys
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Optional, Dict, Any, Callable
import logging
from pathlib import Path

_parent_dir = str(Path(__file__).parent.parent)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

from core.adb_controller import ADBController
from core.screen_capture import ScreenCapture
from core.template_matcher import TemplateMatcher, MatchResult
from core.input_handler import InputHandler


class BotState(Enum):
    IDLE = auto()
    RUNNING = auto()
    STOPPED = auto()


class MapleStoryIdleBot:
    """
    MapleStory Idle Bot - Freestyle detection.

    Templates:
    - app_button: Open the game
    - main_menu: Menu button (top-right)
    - pq_button: Party Quest button
    - sleepywood/ludibrium/orbis: Quest selection
    - start_queue: Start queue button
    - in_queue: Waiting in queue
    - stop_queue: Cancel queue button
    - confirm: OK/Confirm (PRIORITY!)
    - loading_screen, loading_screen2, loading_screen3, loading_screen4, loading_screen5: Game loading
    - sleepywood_wave_1, sleepywood_wave_2, sleepywood_wave_3: In PQ (Sleepywood waves)
    - ludibrium_wave_11, ludibrium_wave_22, ludibrium_wave_33: In PQ (Ludibrium waves)
    - orbis_wave_1, orbis_wave_2, orbis_wave_3: In PQ (Orbis waves)
    - clear: PQ complete indicator (triggers PQ finish)
    - failed: PQ failed mid-run indicator (triggers recovery)
    - red_alert: Boss red attack indicator (wave 3 only) - triggers immediate double-jump
    - jump: Jump button for avoiding attacks
    """

    POSITIONS = {
        "center": (480, 270),
        "main_menu": (900, 50),
        "pq_button": (480, 400),
        "sleepywood": (300, 350),
        "ludibrium": (480, 350),
        "orbis": (660, 350),
        "start_queue": (480, 450),
        "stop_queue": (750, 480),
        "confirm": (480, 400),
    }

    def __init__(self, adb: ADBController, config: Dict[str, Any], logger: Optional[logging.Logger] = None):
        self.adb = adb
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

        self.screen = ScreenCapture(adb, logger)
        self.matcher = TemplateMatcher(
            templates_dir=config.get("templates_dir", "templates/maple_story_idle"),
            logger=logger
        )
        self.input = InputHandler(adb, logger)

        # State
        self.state = BotState.IDLE
        self.running = False
        self.paused = False

        # Stats
        self.pq_runs = 0
        self.queue_timeouts = 0
        self.start_time: Optional[datetime] = None

        # Config
        self.queue_timeout = config.get("queue-timeout", 30)  # seconds
        self.quest_choice = config.get("quest-choice", "sleepywood")
        self.solo_mode = config.get("solo-option", False)  # Solo mode setting

        # Tracking
        self.queue_start_time: Optional[datetime] = None
        self.in_queue = False
        self.in_pq = False
        self.current_wave = 0

        # Orbis wave 1 tracking
        self.orbis_box_found = False
        self.orbis_talk_found = False
        self.orbis_box_target: Optional[MatchResult] = None
        self.orbis_scan_direction = "left"
        self.orbis_wave1_phase = "init"
        self.orbis_phase_started_at: Optional[datetime] = None
        self.orbis_last_move_at: Optional[datetime] = None
        self.orbis_last_action_at: Optional[datetime] = None
        self.orbis_target_lock: Optional[MatchResult] = None

        # Watchdog - restart if stuck for too long
        self.stuck_timeout = config.get("stuck-timeout", 120)  # 2 minutes default
        self.soft_stuck_timeout = self.stuck_timeout // 2
        self.last_activity_time: Optional[datetime] = None
        self.soft_recovery_attempted = False
        self.restarts = 0
        self.recoveries = 0

        # Track consecutive queue timeouts
        self.consecutive_queue_timeouts = 0
        self.max_consecutive_timeouts = config.get("max-queue-timeouts", 5)

        # Track time since last PQ entry
        self.last_pq_entry_time: Optional[datetime] = None
        self.pq_timeout_levels = [450, 900]  # 7.5min, 15min
        self.current_pq_timeout_level = 0

        # Random actions (jump during PQ)
        self.random_actions = config.get("random-jump", False)
        self.jump_interval = config.get("jump-interval", 30)
        self.last_jump_time: Optional[datetime] = None

        # Hard reset tracking
        self.hard_resets = 0

        # Game package name for force-stop
        self.game_package = config.get("game-package", "com.nexon.maplem.global")

        # Callbacks
        self.on_state_change: Optional[Callable] = None
        self.on_stats_update: Optional[Callable] = None
        self.on_log: Optional[Callable] = None

        # Duplicate log prevention
        self.last_log_message: Optional[str] = None

    def _reset_orbis_wave1_state(self):
        self.orbis_box_found = False
        self.orbis_talk_found = False
        self.orbis_box_target = None
        self.orbis_scan_direction = "left"
        self.orbis_wave1_phase = "init"
        self.orbis_phase_started_at = None
        self.orbis_last_move_at = None
        self.orbis_last_action_at = None
        self.orbis_target_lock = None

    def _log(self, msg: str):
        if msg == self.last_log_message:
            return
        self.last_log_message = msg

        self.logger.info(msg)
        if self.on_log:
            self.on_log(msg)

    def _update_stats(self):
        if self.on_stats_update:
            status = "IN PQ" if self.in_pq else ("QUEUED" if self.in_queue else "RUNNING")
            self.on_stats_update({
                "pq_runs": self.pq_runs,
                "runtime": str(datetime.now() - self.start_time).split('.')[0] if self.start_time else "00:00:00",
                "state": status
            })

    def _activity(self):
        """Mark that meaningful activity happened (resets stuck timer)."""
        self.last_activity_time = datetime.now()
        self.soft_recovery_attempted = False

    def _check_stuck(self, screen) -> bool:
        """
        Multi-tier stuck detection:
        - Too long without PQ entry: Force restart
        - At half timeout: Try soft recovery
        - At full timeout: Restart app
        """
        if self.last_activity_time is None:
            self._activity()
            return False

        if self.last_pq_entry_time:
            current_timeout = self.pq_timeout_levels[self.current_pq_timeout_level]
            time_without_pq = (datetime.now() - self.last_pq_entry_time).total_seconds()
            if time_without_pq >= current_timeout:
                self._log(f"!!! NO PQ FOR {int(time_without_pq)}s ({int(time_without_pq/60)}min) - Hard reset !!!")
                self._hard_reset_app()
                return True

        elapsed = (datetime.now() - self.last_activity_time).total_seconds()

        if elapsed >= self.stuck_timeout:
            self._log(f"!!! HARD STUCK for {int(elapsed)}s - Restarting app !!!")
            self._restart_app()
            return True

        if elapsed >= self.soft_stuck_timeout and not self.soft_recovery_attempted:
            self._log(f"!!! SOFT STUCK for {int(elapsed)}s - Attempting recovery !!!")
            self.soft_recovery_attempted = True
            if self._try_recovery(screen):
                return True

        return False

    def _try_recovery(self, screen) -> bool:
        """
        Actively scan for any actionable template and act on it.
        """
        self.recoveries += 1
        self._log(f"Recovery #{self.recoveries} - Scanning all templates...")

        if self.in_pq:
            self._log("Resetting PQ state...")
            self.in_pq = False
            self.current_wave = 0

        emergency_templates = [
            ("lost_connection", "click"),
            ("exit", "click"),
            ("event", "click"),
        ]

        if not self.solo_mode:
            emergency_templates.append(("leave_party", "click"))

        emergency_templates.extend([
            ("clear", "click"),
            ("confirm", "click"),
            ("start_queue", "click"),
            (self.quest_choice, "click"),
            ("pq_button", "click"),
            ("main_menu", "click"),
            ("app_button", "click"),
            ("stop_queue", "click"),
        ])

        for template, action in emergency_templates:
            match = self.matcher.find(screen, template)
            if match:
                self._log(f"Recovery: Found '{template}' - clicking!")
                self.input.tap_center(match)
                self._activity()
                time.sleep(1)
                return True

        self._log("Recovery: No template found, tapping center...")
        self.input.tap(*self.POSITIONS["center"])
        time.sleep(1)
        return False

    def _restart_app(self):
        """Close app and reset state to start fresh."""
        self.restarts += 1
        self._log(f"Restart #{self.restarts} - Closing app...")

        for _ in range(5):
            self.input.press_back()
            time.sleep(0.3)

        self.input.press_home()
        time.sleep(1)

        self.in_pq = False
        self.in_queue = False
        self.current_wave = 0
        self.queue_start_time = None
        self.consecutive_queue_timeouts = 0
        self._reset_orbis_wave1_state()

        self._activity()
        self._log("App closed. Will restart from app_button detection...")
        time.sleep(2)

    def _hard_reset_app(self):
        """Force-stop app via ADB and restart."""
        self.hard_resets += 1
        self._log(f"!!! HARD RESET #{self.hard_resets} - Killing app via Recent Apps !!!")

        self.input.press_home()
        time.sleep(1)

        self.adb.key_event(187)
        self._log("Opened Recent Apps")
        time.sleep(1)

        screen = self.screen.capture(use_cache=False)
        if screen is not None:
            clear_all = self.matcher.find(screen, "clear_all")
            if clear_all:
                self.input.tap_center(clear_all)
                self._log("Clicked CLEAR ALL")
                time.sleep(1)
            else:
                self._log("CLEAR ALL not found - tapping common position")
                self.input.tap(480, 500)
                time.sleep(1)

        self.input.press_home()
        time.sleep(1)

        try:
            self.adb.shell(f"am force-stop {self.game_package}")
        except Exception:
            pass

        time.sleep(1)

        self.in_pq = False
        self.in_queue = False
        self.current_wave = 0
        self.queue_start_time = None
        self.consecutive_queue_timeouts = 0
        self.last_pq_entry_time = datetime.now()
        self._reset_orbis_wave1_state()

        if self.current_pq_timeout_level < len(self.pq_timeout_levels) - 1:
            self.current_pq_timeout_level += 1
        next_timeout = self.pq_timeout_levels[self.current_pq_timeout_level]

        self._activity()
        self._log(f"Apps cleared. Next PQ timeout: {next_timeout//60}min. Looking for app_button...")
        time.sleep(2)

    def start(self):
        self._log("=" * 40)
        self._log("  MapleStory Idle Bot")
        self._log("=" * 40)
        self._log(f"Quest: {self.quest_choice}")
        self._log(f"Queue timeout: {self.queue_timeout}s")
        self._log(f"Stuck timeout: {self.stuck_timeout}s")

        self.running = True
        self.start_time = datetime.now()
        self.state = BotState.RUNNING
        self._activity()
        self.last_pq_entry_time = datetime.now()

        if self.on_state_change:
            self.on_state_change(self.state)

        loaded = self.matcher.preload_templates()
        self._log(f"Loaded {loaded} templates")

        try:
            while self.running:
                if self.paused:
                    time.sleep(0.3)
                    continue
                self._tick()
                time.sleep(0.2)
        except Exception as e:
            self._log(f"Error: {e}")
        finally:
            self.stop()

    def stop(self):
        self.running = False
        self.state = BotState.STOPPED
        if self.on_state_change:
            self.on_state_change(self.state)
        self._log(f"Stopped. Runs: {self.pq_runs}, Timeouts: {self.queue_timeouts}")

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def _tick(self):
        """Main tick - detect and act."""
        self._update_stats()

        screen = self.screen.capture(use_cache=False)
        if screen is None:
            return

        if self._check_and_click(screen, "lost_connection"):
            self._log("!!! LOST CONNECTION - Clicking OK !!!")
            self._activity()
            self.in_pq = False
            self.in_queue = False
            self.current_wave = 0
            self.consecutive_queue_timeouts = 0
            self._reset_orbis_wave1_state()
            time.sleep(2)
            return

        if self._check_and_click(screen, "event"):
            self._log("Event popup - closing")
            time.sleep(1)
            return

        if not self.solo_mode:
            if self._check_and_click(screen, "leave_party"):
                self._log("In party mode - leaving party")
                time.sleep(1)
                return

        if self._check_stuck(screen):
            return

        if self.in_pq:
            self._activity()

            if self.current_wave == 3:
                self._check_red_alert(screen)

            if self.matcher.find(screen, "failed"):
                self._log("!!! PQ FAILED - Recovering !!!")
                self.in_pq = False
                self.current_wave = 0
                self._reset_orbis_wave1_state()
                self._activity()
                self.input.tap(*self.POSITIONS["center"])
                time.sleep(1.5)

                new_screen = self.screen.capture(use_cache=False)
                if new_screen is not None:
                    if self._check_and_click(new_screen, "start_queue"):
                        self._log("Restarting queue after failure")
                        self.in_queue = True
                        self.queue_start_time = datetime.now()
                    else:
                        self.input.tap(*self.POSITIONS["center"])

                time.sleep(1)
                return

            if self._check_and_click(screen, "start_queue"):
                self._log("!!! PQ FAILED (detected via start_queue) - Restarting queue !!!")
                self.in_pq = False
                self.current_wave = 0
                self._reset_orbis_wave1_state()
                self._activity()
                self.in_queue = True
                self.queue_start_time = datetime.now()
                time.sleep(1)
                return

            if self.matcher.find(screen, "clear"):
                self._log("PQ finished!")
                self.in_pq = False
                self.current_wave = 0
                self.pq_runs += 1
                self.consecutive_queue_timeouts = 0
                self._reset_orbis_wave1_state()
                self._log(f"=== PQ #{self.pq_runs} Complete! ===")
                time.sleep(1)
                return

            wave = self._check_wave(screen)
            if wave > 0 and wave != self.current_wave:
                self._log(f"Wave {wave}")
                self.current_wave = wave
                if self.current_wave != 1:
                    self._reset_orbis_wave1_state()

            if self.current_wave == 3:
                self._check_red_alert(screen)

            self._try_jump(screen)

            if self.quest_choice == "orbis" and self.current_wave in [2, 3]:
                if self._check_and_click(screen, "orbis_talk"):
                    self._log("Found orbis_talk - clicking!")
                    self._activity()
                    time.sleep(1)
                    return

            if self.quest_choice == "orbis" and self.current_wave == 1:
                if self._handle_orbis_wave_1(screen):
                    return

            time.sleep(0.5)
            return

        if self._check_and_click(screen, "confirm"):
            self._log(">>> CONFIRM clicked!")
            self._activity()
            time.sleep(1)
            return

        wave = self._check_wave(screen)
        if wave > 0:
            self._log(f"Entered PQ! Wave {wave}")
            self.in_pq = True
            self.in_queue = False
            self.current_wave = wave
            self.last_pq_entry_time = datetime.now()
            self.last_jump_time = None
            if self.quest_choice == "orbis" and wave == 1:
                self._reset_orbis_wave1_state()
            self._activity()
            time.sleep(3)
            return

        if self.solo_mode:
            if self._create_party_if_needed(screen):
                if self._start_solo_pq(screen):
                    return
        else:
            if self.in_queue:
                self._handle_queue(screen)
                return

        self._detect_and_act(screen)

    def _check_wave(self, screen) -> int:
        """Check if any wave indicator is visible. Returns wave number or 0."""
        if self.quest_choice == "ludibrium":
            if self.matcher.find(screen, "ludibrium_wave_33"):
                return 3
            if self.matcher.find(screen, "ludibrium_wave_22"):
                return 2
            if self.matcher.find(screen, "ludibrium_wave_11"):
                return 1
        elif self.quest_choice == "orbis":
            if self.matcher.find(screen, "orbis_wave_3"):
                return 3
            if self.matcher.find(screen, "orbis_wave_2"):
                return 2
            if self.matcher.find(screen, "orbis_wave_1"):
                return 1
        elif self.quest_choice == "sleepywood":
            if self.matcher.find(screen, "sleepywood_wave_3"):
                return 3
            if self.matcher.find(screen, "sleepywood_wave_2"):
                return 2
            if self.matcher.find(screen, "sleepywood_wave_1"):
                return 1
        return 0

    def _get_queue_template(self) -> str:
        return "in_queue_ludi" if self.quest_choice == "ludibrium" else "in_queue"

    def _check_and_click(self, screen, template: str) -> bool:
        match = self.matcher.find(screen, template)
        if match:
            self.input.tap_center(match)
            return True
        return False

    def _try_jump(self, screen):
        """
        Try to double-tap jump button during PQ (if random_actions enabled).
        Only jumps every jump_interval seconds.
        """
        if not self.random_actions:
            return

        if self.last_jump_time:
            elapsed = (datetime.now() - self.last_jump_time).total_seconds()
            if elapsed < self.jump_interval:
                return

        match = self.matcher.find(screen, "jump")
        if match:
            self._log("Jumping!")
            self.input.tap_center(match)
            time.sleep(0.1)
            self.input.tap_center(match)
            self.last_jump_time = datetime.now()

    def _check_red_alert(self, screen) -> bool:
        """
        Check for red alert during wave 3 and immediately double-jump.
        """
        if self.current_wave != 3:
            return False

        # Check for any red alert template
        red_alert_templates = []
        if self.quest_choice == "orbis":
            red_alert_templates = ["red_alert_orbis_1", "red_alert_orbis_2", "red_alert_orbis"]
        else:
            red_alert_templates = ["red_alert"]
        
        red_alert = None
        detected_template = None
        for template in red_alert_templates:
            match = self.matcher.find(screen, template)
            if match:
                red_alert = match
                detected_template = template
                break

        if not red_alert:
            return False

        self._log(f"!!! RED ALERT DETECTED ({detected_template}) - Confidence: {red_alert.confidence:.3f} - JUMPING !!!")

        jump = self.matcher.find(screen, "jump")
        if jump:
            self.input.tap_center(jump)
            time.sleep(0.05)
            self.input.tap_center(jump)
            self.last_jump_time = datetime.now()
            return True
        else:
            self._log("Jump button not found!")
            return False

    def _create_party_if_needed(self, screen) -> bool:
        """
        Create a party if not already in one (solo mode).
        """
        self._log("Checking party status...")

        if self.matcher.find(screen, "party_info"):
            self._log("Already in a party")
            return True

        create_party = self.matcher.find(screen, "create_party")
        if create_party:
            self._log("Creating party...")
            self.input.tap_center(create_party)
            self._activity()
            time.sleep(1)
            return True
        else:
            self._log("Neither in party nor create_party button found")
            return False

    def _start_solo_pq(self, screen) -> bool:
        """
        Start PQ in solo mode using ENTER and CONFIRM buttons.
        """
        enter = self.matcher.find(screen, "enter")
        if enter:
            self._log("Solo mode: Clicking ENTER to start PQ")
            self.input.tap_center(enter)
            self._activity()
            time.sleep(0.5)

            confirm = self.matcher.find(screen, "confirm")
            if confirm:
                self._log("Solo mode: Clicking CONFIRM to start PQ")
                self.input.tap_center(confirm)
                self._activity()
                time.sleep(1)
                return True
            else:
                self._log("ENTER clicked but no CONFIRM button found")
                return False
        else:
            self._log("No ENTER button found for solo PQ start")
            return False

    def _handle_orbis_wave_1(self, screen) -> bool:
        """
        Orbis wave 1 handler.

        Behavior:
        - Jump up once
        - Jump left once
        - Pause and look
        - Only move when a box is actually detected
        - Do NOT auto-scan right
        """
        now = datetime.now()

        # Highest priority: talk prompt
        orbis_talk = self.matcher.find(screen, "orbis_talk")
        if orbis_talk:
            self._log("Orbis wave 1: talk")
            self.input.tap_center(orbis_talk)
            self.orbis_target_lock = None
            self.orbis_last_action_at = now
            self._activity()
            return True

        # Phase 1: initial jump up
        if self.orbis_wave1_phase == "init":
            self._log("Orbis wave 1: init jump up")
            self._perform_double_jump(direction="up", hold_time=0.22, tap_gap=0.06)
            self.orbis_wave1_phase = "wait_for_target"
            self.orbis_phase_started_at = now
            self._activity()
            return True

        # Phase 3: settle briefly after left jump
        if self.orbis_wave1_phase == "wait_for_target":
            if self.orbis_phase_started_at and (now - self.orbis_phase_started_at).total_seconds() < 0.25:
                return True

            # Look for a box, but do not move right automatically
            orbis_box = self.matcher.find(screen, "orbis_box")
            if orbis_box:
                self.orbis_target_lock = orbis_box
                self.orbis_wave1_phase = "target"
                return True

            # No target yet: stay still
            return True

        # Phase 4: move only toward detected target
        if self.orbis_wave1_phase == "target":
            target = self.orbis_target_lock
            if target is None:
                self.orbis_wave1_phase = "wait_for_target"
                return True

            char_x = self.input.screen_width // 2
            char_y = self.input.screen_height // 2

            dx = target.center_x - char_x
            dy = target.center_y - char_y

            self._log(f"Orbis target dx={dx}, dy={dy}")

            # Box above
            if dy < -40:
                if abs(dx) > 80:
                    direction = "left" if dx < 0 else "right"
                    self._perform_double_jump(direction=direction, hold_time=0.20, tap_gap=0.06)
                else:
                    self._perform_double_jump(direction="up", hold_time=0.18, tap_gap=0.06)

                self.orbis_target_lock = None
                self.orbis_wave1_phase = "wait_for_target"
                self._activity()
                return True

            # Box below
            if dy > 50:
                if dx < -50:
                    self.input.move_left(0.05)
                    self._activity()
                    return True
                elif dx > 50:
                    self.input.move_right(0.05)
                    self._activity()
                    return True
                else:
                    self.adb.key_down(20)  # down
                    jump = self.matcher.find(screen, "jump")
                    if jump:
                        self.input.tap_center(jump)
                    else:
                        self.input.character_jump()
                    self.adb.key_up(20)

                    self.orbis_target_lock = None
                    self.orbis_wave1_phase = "wait_for_target"
                    self._activity()
                    return True

            # Same level
            if dx < -40:
                self.input.move_left(0.05)
                self._activity()
                return True
            elif dx > 40:
                self.input.move_right(0.05)
                self._activity()
                return True
            else:
                self.orbis_target_lock = None
                self.orbis_wave1_phase = "wait_for_target"
                self._activity()
                return True

        return False
    
    def _should_double_jump_to_box(self, orbis_box) -> bool:
        """
        Check if we should double jump to reach a box.
        """
        char_x = self.input.screen_width // 2
        char_y = self.input.screen_height // 2

        x_diff = abs(orbis_box.center_x - char_x)
        y_diff = char_y - orbis_box.center_y
        self._log(
            f"Double jump check: Box at ({orbis_box.center_x}, {orbis_box.center_y}), "
            f"Char at ({char_x}, {char_y}), X diff: {x_diff}, Y diff: {y_diff}"
        )

        if orbis_box.center_y < char_y:
            max_x_distance = 300
            max_y_distance = 400

            if x_diff <= max_x_distance and y_diff <= max_y_distance:
                self._log("Double jump condition met! Box is above and within jumping distance.")
                return True
            else:
                self._log(
                    f"Box is above but too far (X diff: {x_diff} > {max_x_distance} "
                    f"or Y diff: {y_diff} > {max_y_distance})"
                )
        else:
            self._log(f"Box is not above character (box_y: {orbis_box.center_y} >= char_y: {char_y})")

        return False

    def _perform_double_jump(self, direction: str = "up", hold_time: float = 0.22, tap_gap: float = 0.06) -> bool:
        """
        Faster double jump with shorter hold timing.
        direction: 'up', 'left', 'right'
        """
        self._log(f"Double jumping ({direction})...")
        import threading

        DPAD_UP = 19
        DPAD_LEFT = 21
        DPAD_RIGHT = 22

        def hold_keys():
            self.adb.key_down(DPAD_UP)

            if direction == "left":
                self.adb.key_down(DPAD_LEFT)
            elif direction == "right":
                self.adb.key_down(DPAD_RIGHT)

            time.sleep(hold_time)

            self.adb.key_up(DPAD_UP)
            if direction == "left":
                self.adb.key_up(DPAD_LEFT)
            elif direction == "right":
                self.adb.key_up(DPAD_RIGHT)

        t = threading.Thread(target=hold_keys, daemon=True)
        t.start()

        time.sleep(0.03)

        jump_screen = self.screen.capture(use_cache=False)
        jump = self.matcher.find(jump_screen, "jump") if jump_screen is not None else None
        if jump:
            self.input.tap_center(jump)
            time.sleep(tap_gap)
            self.input.tap_center(jump)
        else:
            self.input.character_jump()
            time.sleep(tap_gap)
            self.input.character_jump()

        t.join()
        return True

    def _handle_queue(self, screen):
        """Handle being in queue."""
        if self.queue_start_time:
            elapsed = (datetime.now() - self.queue_start_time).total_seconds()

            if elapsed >= self.queue_timeout:
                self._log(f"Queue timeout ({self.queue_timeout}s)! Canceling...")
                self.queue_timeouts += 1
                self.consecutive_queue_timeouts += 1

                if self.consecutive_queue_timeouts >= self.max_consecutive_timeouts:
                    self._log(f"!!! {self.consecutive_queue_timeouts} consecutive queue timeouts - Restarting app !!!")
                    self._restart_app()
                    return

                self._cancel_queue(screen)
                return

            if int(elapsed) % 10 == 0 and int(elapsed) > 0:
                self._log(f"Queue: {int(elapsed)}s / {self.queue_timeout}s")

        if self.matcher.find(screen, self._get_queue_template()):
            return

        if self._check_wave(screen):
            self._log("PQ starting!")
            self.in_queue = False
            self.in_pq = True
            self.consecutive_queue_timeouts = 0
            self.last_pq_entry_time = datetime.now()
            self.last_jump_time = None
            self._activity()
            return

        if self.matcher.find(screen, "stop_queue"):
            return

        self._log("Left queue")
        self.in_queue = False
        self._detect_and_act(screen)

    def _cancel_queue(self, screen):
        """Cancel the queue."""
        self.in_queue = False

        if self._check_and_click(screen, "stop_queue"):
            self._log("Clicked stop queue")
            time.sleep(1)
            return

        self.input.tap(*self.POSITIONS["stop_queue"])
        time.sleep(0.3)
        self.input.press_back()
        time.sleep(1)

    def _detect_and_act(self, screen):
        """Detect where we are and take action."""
        if (
            self.matcher.find(screen, "loading_screen") or
            self.matcher.find(screen, "loading_screen2") or
            self.matcher.find(screen, "loading_screen3") or
            self.matcher.find(screen, "loading_screen4") or
            self.matcher.find(screen, "loading_screen5")
        ):
            self._log("Loading...")
            self._activity()
            time.sleep(1)
            return

        if self.matcher.find(screen, self._get_queue_template()):
            if not self.in_queue:
                self._log("Now in queue!")
                self.in_queue = True
                self.queue_start_time = datetime.now()
                self._activity()
            return

        if not self.solo_mode:
            if self._check_and_click(screen, "start_queue"):
                self._log("Clicking START QUEUE")
                self.in_queue = True
                self.queue_start_time = datetime.now()
                self._activity()
                time.sleep(1)
                return

        quest = self.quest_choice
        if self.matcher.find(screen, quest):
            self._log(f"Selecting {quest}...")
            self._check_and_click(screen, quest)
            self._activity()
            time.sleep(1)
            return

        if self._check_and_click(screen, "pq_button"):
            self._log("Clicking PQ button")
            self._activity()
            time.sleep(1)
            return

        if self._check_and_click(screen, "main_menu"):
            self._log("Opening main menu")
            self._activity()
            time.sleep(2.5)
            return

        if self._check_and_click(screen, "app_button"):
            self._log("Opening game")
            self._activity()
            time.sleep(3)
            return

        if self.matcher.find(screen, "stop_queue"):
            self._log("In queue (stop visible)")
            self.in_queue = True
            self._activity()
            if not self.queue_start_time:
                self.queue_start_time = datetime.now()
            return

        self._log("Unknown screen, tapping...")
        self.input.tap(*self.POSITIONS["center"])
        time.sleep(2)

    def get_stats(self) -> Dict[str, Any]:
        return {
            "state": self.state.name,
            "pq_runs": self.pq_runs,
            "queue_timeouts": self.queue_timeouts,
            "recoveries": self.recoveries,
            "restarts": self.restarts,
            "hard_resets": self.hard_resets,
            "runtime": str(datetime.now() - self.start_time).split('.')[0] if self.start_time else "00:00:00",
            "in_queue": self.in_queue,
            "in_pq": self.in_pq,
            "current_wave": self.current_wave,
            "running": self.running
        }