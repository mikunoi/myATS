from src.database.store import Store


class RecoveryGate:
    def __init__(self, store: Store):
        self.store = store
        self.reconciled = False

    def disconnected(self):
        self.reconciled = False

    def reconcile(self, broker, coordinator=None):
        self.reconciled = False
        broker.restore_completed_state()
        if coordinator is not None:
            coordinator.restore_strategy_states()
        broker.snapshot()
        self.reconciled = True
        return True

    def orders_allowed(self, *, is_buy: bool) -> bool:
        state = self.store.control()
        return self.reconciled and (state == "RUNNING" or (state == "PAUSE" and not is_buy))
