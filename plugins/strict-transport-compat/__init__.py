import weakref

from .yuanbao import adapt


def register(ctx):
    applied = weakref.WeakKeyDictionary()

    def wire(native, adapter):
        undo = adapt(native, adapter)
        if undo is not None:
            applied[adapter] = undo

    def unload():
        for undo in list(applied.values()):
            undo()
        applied.clear()

    ctx.register_platform_handler('yuanbao', wire)
    ctx.on_unload(unload)
