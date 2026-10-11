"""Account lifetime, separate from UI and export. Never persists a 2FA password."""
import asyncio

from .telegram import Telegram


class Account:
    def __init__(self, vault, factory=Telegram):
        self.vault, self.factory = vault, factory
        self.telegram = None
        self.credentials = None
        self.remember = False

    async def open(self, api_id, api_hash, remember, session=""):
        if self.telegram is not None:
            raise RuntimeError("An account is already open")
        self.remember = remember
        self.credentials = (api_id, api_hash)
        self.telegram = self.factory(api_id, api_hash, session)
        return await self.telegram.connect()

    def save(self):
        if self.remember:
            self.vault.save(self.credentials[0], self.telegram.session_string())
        else:
            self.vault.delete()

    async def close(self, forget=False, signed_in=True):
        """Return whether remote logout was confirmed when logout was required. Without a sign-in there is
        nothing to end in Telegram: only the connection and the local copy go."""
        telegram = self.telegram
        confirmed = True
        try:
            if telegram and signed_in:
                if forget or not self.remember:
                    try:
                        confirmed = await telegram.logout()
                    except (Exception, asyncio.CancelledError):
                        # Boundary: report no confirmation, never expose exception contents.
                        confirmed = False
                else:
                    self.save()
        finally:
            try:
                if telegram:
                    try:
                        await telegram.disconnect()
                    except (Exception, asyncio.CancelledError):
                        confirmed = False
            finally:
                self.telegram = None
                self.credentials = None
                if forget or not self.remember:
                    self.vault.delete()
                self.remember = False
        return confirmed

    async def abandon(self, signed_in=True):
        """Used after cancelled/failed authentication, including partially successful QR. When Telegram
        refused the application itself, nothing was signed in (`signed_in=False`): no logout to confirm."""
        return await asyncio.shield(self.close(forget=True, signed_in=signed_in))
