import threading

from modules.interaction.public import InteractionFacade


def run_reminders(agent: InteractionFacade, stop: threading.Event) -> None:
    while not stop.is_set():
        agent.remind_schedule()
        agent.remind_tasks()
        stop.wait(15)
