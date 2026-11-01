"""The endpoints only product searches have: taking back a word hiding taught a category.
app.py includes ROUTER through kinds.ROUTERS."""

from fastapi import APIRouter

from . import learning
from .models import Forget

ROUTER = APIRouter()


@ROUTER.post('/api/learned/forget')
async def forget(payload: Forget):
    """A learned word no longer hides offers of its category; the next check shows them again."""
    learning.forget(payload.category, payload.word)
    return {'ok': True}
