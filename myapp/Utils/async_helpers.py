"""
Small helpers for async DRF views.

DRF serializer methods (`is_valid`, `save`) run validators that hit the
database synchronously. In an ASGI view you must wrap them in
`sync_to_async` or Django raises `SynchronousOnlyOperation`.
"""
from asgiref.sync import sync_to_async


async def async_is_valid(serializer, raise_exception=True):
    """Async wrapper around DRF's `serializer.is_valid()`."""
    return await sync_to_async(
        serializer.is_valid, thread_sensitive=True,
    )(raise_exception=raise_exception)


async def async_save(serializer, **kwargs):
    """Async wrapper around DRF's `serializer.save()`."""
    return await sync_to_async(
        serializer.save, thread_sensitive=True,
    )(**kwargs)


async def async_validate_and_save(serializer, **save_kwargs):
    """Common shortcut: validate (raise on error) and save."""
    await async_is_valid(serializer, raise_exception=True)
    return await async_save(serializer, **save_kwargs)
