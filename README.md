# wcpan.drive

Asynchronous generic cloud drive API.

This package needs a driver to actually work with a cloud drive.

## Example Usage

```python
from wcpan.drive import DriveFactory, download_to_local


async def api_demo():
    # setup environment
    factory = DriveFactory()
    # read config file from here
    # default is $HOME/.config/wcpan/drive
    factory.set_config_path('/tmp/config')
    # put data file to here
    # default is $HOME/.local/share/wcpan/drive
    factory.set_data_path('/tmp/data')
    # setup cache database, will write to data folder
    factory.set_database('nodes.sqlite')
    # setup driver module
    factory.set_driver('wcpan.drive.google')
    # load config file from config folder
    # this will not overwrite given values
    factory.load_config()

    async with factory.create_drive() as drive:
        # it is important to keep cache in sync
        async for change in drive.sync():
            print(change)

        # download file
        node = await drive.get_node_by_path('/path/to/drive/file')
        download_to_local(drive, node, '/tmp')
```

## Command Line Usage

```sh
python3 -m wcpan.drive -h
```
