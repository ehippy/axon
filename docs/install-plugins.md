# Install agent plugins

Build/load just the plugins on agent hosts; no journal server or handset code is
needed. Follow [the plugin guide](../plugins/README.md) for native packages or the
managed installer. The reporter can also be installed as a standalone CLI; see
[its guide](../packages/python/axon_reporter/README.md). To keep agent hosts
current automatically (clone + `manage.py sync --apply` from a bootstrap
loop), follow [Keeping machines current](../plugins/README.md#keeping-machines-current).
