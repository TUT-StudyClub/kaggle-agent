import threading

from modules.interaction.public import InteractionFacade
from shared.application.ports.clock import Clock


def run_worker(
    agent: InteractionFacade, stop: threading.Event, retention_days: int, clock: Clock
) -> None:
    last_maintenance = 0.0
    while not stop.is_set():
        if clock.monotonic() - last_maintenance > 60:
            agent.recover()
            agent.purge(clock.timestamp() - retention_days * 86400)
            last_maintenance = clock.monotonic()
        agent.deliver_notifications()
        if not agent.run_once():
            stop.wait(0.5)
