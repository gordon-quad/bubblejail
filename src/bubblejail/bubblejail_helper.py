# SPDX-License-Identifier: GPL-3.0-or-later

# Copyright 2019-2022 igo95862

# This file is part of bubblejail.
# bubblejail is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# bubblejail is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# You should have received a copy of the GNU General Public License
# along with bubblejail.  If not, see <https://www.gnu.org/licenses/>.
from __future__ import annotations

from argparse import REMAINDER as ARG_REMAINDER
from argparse import ArgumentParser
from asyncio import (
    CancelledError,
    Event,
    create_subprocess_exec,
    create_task,
    new_event_loop,
    sleep,
    start_unix_server,
    wait_for,
)
from asyncio.subprocess import DEVNULL, PIPE, STDOUT
from collections.abc import Awaitable
from json import dumps as json_dumps
from json import loads as json_loads
from os import WNOHANG, kill, wait3, waitpid
from pathlib import Path
from signal import SIGCHLD, SIGKILL, SIGTERM
from socket import AF_UNIX, socket
from time import sleep as sync_sleep
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from asyncio import AbstractServer, StreamReader, StreamWriter, Task
    from collections.abc import Generator
    from typing import Any, Literal

    RpcMethods = Literal['ping', 'run']
    RpcData = dict[str, bool | str | list[str]] | list[str]
    RpcType = dict[str, str | RpcData | RpcMethods | None]


class JsonRpcRequest:
    @staticmethod
    def _dict_to_json_byte_line(rpc_dict: dict[Any, Any]) -> bytes:
        string_form = json_dumps(rpc_dict) + '\n'
        return string_form.encode()

    @staticmethod
    def _json_byte_line_to_dict(data: bytes) -> dict[Any, Any]:
        decoded_dict: dict[Any, Any] = json_loads(data)
        # Replace 'id' with 'request_id'
        decoded_dict['request_id'] = decoded_dict['id']
        decoded_dict.pop('id')

        return decoded_dict

    def __init__(self,
                 method: RpcMethods,
                 request_id: str | None = None,
                 params: RpcData | None = None,
                 ):
        self.request_id = request_id
        self.method = method
        self.params = params

    def to_json_byte_line(self) -> bytes:
        dict_form = {
            "id": self.request_id,
            "method": self.method,
            "params": self.params,
        }
        return self._dict_to_json_byte_line(dict_form)

    def _get_reponse_bytes(self, rpc_data: RpcData) -> bytes:
        response_dict = {
            "id": self.request_id,
            "result": rpc_data,
        }
        return self._dict_to_json_byte_line(response_dict)


class RequestPing(JsonRpcRequest):
    def __init__(self, request_id: str | None = None) -> None:
        super().__init__(
            method='ping',
            request_id=request_id,
        )

    def response_ping(self) -> bytes:
        return self._get_reponse_bytes(['pong'])


class RequestRun(JsonRpcRequest):
    def __init__(
        self,
        args_to_run: list[str],
        wait_response: bool = False,
        request_id: str | None = None
    ) -> None:
        super().__init__(
            method='run',
            request_id=request_id,
            params={
                'args_to_run': args_to_run,
                'wait_response': wait_response,
            },
        )
        self.args_to_run = args_to_run
        self.wait_response = wait_response

    def response_run(self, text: str) -> bytes:
        return self._get_reponse_bytes({'return': text})

    def decode_response(self, text: bytes) -> str:
        possible_str = json_loads(text)['result']['return']

        if isinstance(possible_str, str):
            return possible_str
        else:
            raise TypeError('Expected str in response.')


if TYPE_CHECKING:
    RpcRequests = RequestPing | RequestRun


def request_selector(data: bytes) -> RpcRequests:
    decoded_dict = JsonRpcRequest._json_byte_line_to_dict(data)

    method = decoded_dict['method']
    request_id = decoded_dict['request_id']
    params = decoded_dict['params']

    match method:
        case 'ping':
            return RequestPing(request_id=request_id)
        case 'run':
            return RequestRun(
                request_id=request_id,
                **params
            )
        case _:
            raise TypeError('Unknown rpc method.')


def handle_children() -> None:
    """Reaps dead children."""
    # Needs to be in exception
    # Seems like the sometimes python raises ChildProcessError
    # Most often however (0, 0)
    wait_pid_tuple: tuple[int, int] = (0, 0)
    try:
        # Walrus in action
        while (wait_pid_tuple := waitpid(-1, WNOHANG)) != (0, 0):
            exit_pid, exit_code = wait_pid_tuple
            if exit_pid == 0:
                return

            if __debug__:
                print('Reaped: ', exit_pid,
                      ' Exit code: ', exit_code,
                      flush=True)

    except ChildProcessError:
        ...


def terminate_children(run_helper_task: Task[None]) -> None:
    """Send SIGTERM to all children of this process"""
    signal_counter = 0
    pids_sigtermed = set()

    def signal_all_children() -> None:
        nonlocal signal_counter
        signal_counter += 1
        # Send SIGTERM to all our children
        for task_dir in Path('/proc/self/task/').iterdir():
            # Open task
            with open(task_dir / 'children') as children_file:
                children_file_pids = children_file.read().split()
                for pid in (int(pid_str) for pid_str in children_file_pids):
                    if signal_counter > 20:
                        # if we tried to send sigterm 20 times and it still
                        # did not work use SIGKILL
                        kill(pid, SIGKILL)
                        continue

                    if pid not in pids_sigtermed:
                        kill(pid, SIGTERM)
                        pids_sigtermed.add(pid)

    signal_all_children()
    while True:
        # Reap the rest of the children
        # this will block
        # might cause stalls in shutdown
        # might also have race conditions with SIGCHLD
        try:
            pid_reaped, _, _ = wait3(WNOHANG)
            if pid_reaped == 0:
                sync_sleep(0.5)
                signal_all_children()
        except ChildProcessError:
            break

    run_helper_task.cancel()


class BubblejailHelper(Awaitable[bool]):
    def __init__(
        self,
        socket: socket,
        startup_args: list[str],
        no_child_timeout: int | None = 3,
        reaper_pool_timer: int = 5,
        use_fixups: bool = True,
    ):
        self.startup_args = startup_args
        self.socket = socket

        # Server
        self.server: AbstractServer | None = None

        # Event terminated
        self.terminated = Event()

        # Terminator variables
        self.terminator_look_for_command: str | None = None

        self.terminator_pool_timer = reaper_pool_timer
        self.termninator_watcher_task: Task[None] | None = None

        self.ready_event = Event()

        # Fix-ups
        if not use_fixups:
            return

    @classmethod
    def iter_proc_process_directories(cls) -> Generator[Path, None, None]:
        proc_path = Path('/proc')
        # Iterate over items in /proc
        for proc_item in proc_path.iterdir():
            # If we found something without number as name
            # skip it as its not a process
            if proc_item.name.isnumeric():
                yield proc_item

    @classmethod
    def proc_has_process_command(cls, process_command: str) -> bool:
        for process_dir in cls.iter_proc_process_directories():
            # read cmdline file containing cmd arguments
            try:
                with open(process_dir / 'stat') as stat_file:
                    # Read file and split by white space
                    # The command argument is a second white space
                    # separated argument so we only need to split 2 times
                    stat_file_data_list = stat_file.read().split(maxsplit=2)
                    # compare command
                    # The command is enclosed in () round parenthesis
                    # [1:-1] will remove them
                    if process_command == stat_file_data_list[1][1:-1]:
                        return True
            except FileNotFoundError:
                continue

        return False

    @classmethod
    def process_has_child(cls) -> bool:
        for task_dir in Path('/proc/self/task/').iterdir():

            # Open task
            with open(task_dir / 'children') as children_file:
                children_file_contents = children_file.read()
                if children_file_contents:
                    # Children file will be empty if there are not children
                    return True

        return False

    async def termninator_watcher(self) -> None:
        if __debug__:
            print(
                'self.terminator_look_for_command: ',
                repr(self.terminator_look_for_command))

        while True:
            try:
                await sleep(self.terminator_pool_timer)  # wait timer

                if self.terminator_look_for_command is None:
                    is_time_to_termniate = not self.process_has_child()
                else:
                    is_time_to_termniate = not self.proc_has_process_command(
                        self.terminator_look_for_command
                    )

                if is_time_to_termniate:
                    if __debug__:
                        print('No children found. Terminating.')
                    create_task(self.stop_async())
                    return
            except CancelledError:
                return

    async def run_command(
        self,
        args_to_run: list[str],
        std_in_out_mode: int | None = None,
    ) -> str | None:

        if std_in_out_mode == DEVNULL:
            p = await create_subprocess_exec(
                *args_to_run,
                stdout=DEVNULL,
                stderr=DEVNULL,
                stdin=DEVNULL,
            )
        elif std_in_out_mode == PIPE:
            p = await create_subprocess_exec(
                *args_to_run,
                stdout=PIPE,
                stderr=STDOUT,
                stdin=PIPE,
            )
        else:
            p = await create_subprocess_exec(
                *args_to_run,
            )

        if std_in_out_mode is None:
            await p.wait()
        elif std_in_out_mode == PIPE:
            (stdout_data, _) = await p.communicate()
            return stdout_data.decode()

        return None

    async def client_handler(
            self,
            reader: StreamReader,
            writer: StreamWriter) -> None:

        if __debug__:
            print('Client connected', flush=True)

        while True:
            line = await reader.readline()
            if not line:
                if __debug__:
                    print('Reached end of reader. Returnning', flush=True)
                writer.close()
                await writer.wait_closed()
                return

            request = request_selector(line)

            match request:
                case RequestPing():
                    response = request.response_ping()
                case RequestRun(args_to_run=args_to_run,
                                wait_response=wait_response):
                    run_stdout = await self.run_command(
                        args_to_run=args_to_run,
                        std_in_out_mode=PIPE if wait_response else None,
                    )
                    if run_stdout is None:
                        continue

                    response = request.response_run(
                        text=run_stdout,
                    )
                case _:
                    print("Unknown request")
                    continue

            writer.write(response)
            await writer.drain()

    async def start_async(self) -> None:
        self.server = await start_unix_server(
            self.client_handler,
            sock=self.socket,
        )
        if __debug__:
            print('Started unix server', flush=True)
        self.termninator_watcher_task = create_task(self.termninator_watcher())

        await wait_for(self.ready_event.wait(), timeout=3)

        if self.startup_args:
            await self.run_command(
                self.startup_args,
            )

    async def stop_async(self) -> None:

        if (self.termninator_watcher_task is not None
            and
                not self.termninator_watcher_task.done()):
            self.termninator_watcher_task.cancel()
            await self.termninator_watcher_task

        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

        self.terminated.set()

        print('Terminated', flush=True)

    def __await__(self) -> Generator[Any, None, bool]:
        # Pylint does not recognize that we get a coroutine object
        # not the boolean
        # pylint: disable=E1101
        coroutine = self.terminated.wait()
        return coroutine.__await__()


def get_helper_argument_parser() -> ArgumentParser:
    parser = ArgumentParser()

    parser.add_argument(
        '--helper-socket',
        type=int,
        required=True,
    )
    parser.add_argument(
        '--shell',
        action='store_true',
    )
    parser.add_argument(
        "--ready-fd",
        type=int,
    )

    parser.add_argument(
        'args_to_run',
        nargs=ARG_REMAINDER,
    )

    return parser


def bubblejail_helper_main() -> None:
    parser = get_helper_argument_parser()

    parsed_args = parser.parse_args()

    if not parsed_args.shell:
        startup_args = parsed_args.args_to_run
    else:
        startup_args = ['/bin/sh']

    helper = BubblejailHelper(
        socket(AF_UNIX, fileno=parsed_args.helper_socket),
        startup_args=startup_args,
    )

    async def run_helper() -> None:
        await helper.start_async()
        await helper

    event_loop = new_event_loop()
    run_helper_task = event_loop.create_task(run_helper(), name='Run helper')
    event_loop.add_signal_handler(SIGCHLD, handle_children)
    event_loop.add_signal_handler(SIGTERM, terminate_children, run_helper_task)

    if parsed_args.ready_fd is None:
        helper.ready_event.set()
    else:
        def read_ready() -> None:
            event_loop.remove_reader(parsed_args.ready_fd)

            with open(parsed_args.ready_fd) as f:
                f.read()

            helper.ready_event.set()

        event_loop.add_reader(
            parsed_args.ready_fd,
            read_ready,
        )
    try:
        event_loop.run_until_complete(run_helper_task)
    except CancelledError:
        print('Termninated by CancelledError')
    finally:
        event_loop.close()


if __name__ == '__main__':
    bubblejail_helper_main()
