import json
import os
import pathlib
import tempfile
import time
import uuid
from concurrent.futures import CancelledError, TimeoutError
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import gradio as gr
import huggingface_hub
import pytest
import uvicorn
from fastapi import FastAPI
from gradio.networking import Server
from huggingface_hub import HfFolder
from huggingface_hub.utils import RepositoryNotFoundError

from gradio_client import Client
from gradio_client.client import DEFAULT_TEMP_DIR
from gradio_client.utils import Communicator, ProgressUnit, Status, StatusUpdate

HF_TOKEN = os.getenv("HF_TOKEN") or HfFolder.get_token()


@contextmanager
def connect(
    demo: gr.Blocks, serialize: bool = True, output_dir: str = DEFAULT_TEMP_DIR
):
    _, local_url, _ = demo.launch(prevent_thread_lock=True)
    try:
        yield Client(local_url, serialize=serialize, output_dir=output_dir)
    finally:
        # A more verbose version of .close()
        # because we should set a timeout
        # the tests that call .cancel() can get stuck
        # waiting for the thread to join
        if demo.enable_queue:
            demo._queue.close()
        demo.is_running = False
        demo.server.should_exit = True
        demo.server.thread.join(timeout=1)


class TestClientPredictions:
    @pytest.mark.flaky
    def test_raise_error_invalid_state(self):
        with pytest.raises(ValueError, match="invalid state"):
            Client("gradio-tests/paused-space")

    @pytest.mark.flaky
    def test_numerical_to_label_space(self):
        client = Client("gradio-tests/titanic-survival")
        label = json.load(
            open(client.predict("male", 77, 10, api_name="/predict"))  # noqa: SIM115
        )
        assert label["label"] == "Perishes"
        with pytest.raises(
            ValueError,
            match="This Gradio app might have multiple endpoints. Please specify an `api_name` or `fn_index`",
        ):
            client.predict("male", 77, 10)
        with pytest.raises(
            ValueError,
            match="Cannot find a function with `api_name`: predict. Did you mean to use a leading slash?",
        ):
            client.predict("male", 77, 10, api_name="predict")

    @pytest.mark.flaky
    def test_numerical_to_label_space_v4(self):
        client = Client("gradio-tests/titanic-survivalv4-sse")
        label = client.predict("male", 77, 10, api_name="/predict")
        assert label["label"] == "Perishes"

    @pytest.mark.flaky
    def test_private_space(self):
        space_id = "gradio-tests/not-actually-private-space"
        api = huggingface_hub.HfApi()
        assert api.space_info(space_id).private
        client = Client(space_id)
        output = client.predict("abc", api_name="/predict")
        assert output == "abc"

    @pytest.mark.flaky
    def test_private_space_v4(self):
        space_id = "gradio-tests/not-actually-private-spacev4-sse"
        api = huggingface_hub.HfApi()
        assert api.space_info(space_id).private
        client = Client(
            space_id,
        )
        output = client.predict("abc", api_name="/predict")
        assert output == "abc"

    def test_state(self, increment_demo):
        with connect(increment_demo) as client:
            output = client.predict(api_name="/increment_without_queue")
            assert output == 1
            output = client.predict(api_name="/increment_without_queue")
            assert output == 2
            output = client.predict(api_name="/increment_without_queue")
            assert output == 3
            client.reset_session()
            output = client.predict(api_name="/increment_without_queue")
            assert output == 1
            output = client.predict(api_name="/increment_with_queue")
            assert output == 2
            client.reset_session()
            output = client.predict(api_name="/increment_with_queue")
            assert output == 1
            output = client.predict(api_name="/increment_with_queue")
            assert output == 2

    def test_job_status(self, calculator_demo):
        with connect(calculator_demo) as client:
            statuses = []
            job = client.submit(5, "add", 4, api_name="/predict")
            while not job.done():
                time.sleep(0.1)
                statuses.append(job.status())

            assert statuses
            # Messages are sorted by time
            assert sorted([s.time for s in statuses if s]) == [
                s.time for s in statuses if s
            ]
            assert sorted([s.code for s in statuses if s]) == [
                s.code for s in statuses if s
            ]

    @pytest.mark.flaky
    def test_intermediate_outputs(self, count_generator_demo):
        with connect(count_generator_demo) as client:
            job = client.submit(3, fn_index=0)

            while not job.done():
                time.sleep(0.1)

            assert job.outputs() == [str(i) for i in range(3)]

            outputs = []
            for o in client.submit(3, fn_index=0):
                outputs.append(o)
            assert outputs == [str(i) for i in range(3)]

    @pytest.mark.flaky
    def test_intermediate_outputs_with_exception(self, count_generator_demo_exception):
        with connect(count_generator_demo_exception) as client:
            with pytest.raises(Exception):
                client.predict(7, api_name="/count")

            with pytest.raises(
                ValueError, match="Cannot call predict on this function"
            ):
                client.predict(5, api_name="/count_forever")

    def test_break_in_loop_if_error(self, calculator_demo):
        with connect(calculator_demo) as client:
            job = client.submit("foo", "add", 4, fn_index=0)
            output = list(job)
            assert output == []

    @pytest.mark.flaky
    def test_timeout(self, sentiment_classification_demo):
        with pytest.raises(TimeoutError):
            with connect(sentiment_classification_demo.queue()) as client:
                job = client.submit(api_name="/sleep")
                job.result(timeout=0.05)

    @pytest.mark.flaky
    def test_timeout_no_queue(self, sentiment_classification_demo):
        with pytest.raises(TimeoutError):
            with connect(sentiment_classification_demo) as client:
                job = client.submit(api_name="/sleep")
                job.result(timeout=0.1)

    def test_raises_exception(self, calculator_demo):
        with pytest.raises(Exception):
            with connect(calculator_demo) as client:
                job = client.submit("foo", "add", 9, fn_index=0)
                job.result()

    def test_raises_exception_no_queue(self, sentiment_classification_demo):
        with pytest.raises(Exception):
            with connect(sentiment_classification_demo) as client:
                job = client.submit([5], api_name="/sleep")
                job.result()

    def test_job_output_video(self, video_component):
        with connect(video_component) as client:
            job = client.submit(
                {
                    "video": "https://huggingface.co/spaces/gradio/video_component/resolve/main/files/a.mp4"
                },
                fn_index=0,
            )
            assert Path(job.result()["video"]).exists()
            assert (
                Path(DEFAULT_TEMP_DIR).resolve()
                in Path(job.result()["video"]).resolve().parents
            )

        temp_dir = tempfile.mkdtemp()
        with connect(video_component, output_dir=temp_dir) as client:
            job = client.submit(
                {
                    "video": "https://huggingface.co/spaces/gradio/video_component/resolve/main/files/a.mp4"
                },
                fn_index=0,
            )
            assert Path(job.result()["video"]).exists()
            assert (
                Path(temp_dir).resolve()
                in Path(job.result()["video"]).resolve().parents
            )

    def test_progress_updates(self, progress_demo):
        with connect(progress_demo) as client:
            job = client.submit("hello", api_name="/predict")
            statuses = []
            while not job.done():
                statuses.append(job.status())
                time.sleep(0.02)
            assert any(s.code == Status.PROGRESS for s in statuses)
            assert any(s.progress_data is not None for s in statuses)
            all_progress_data = [
                p for s in statuses if s.progress_data for p in s.progress_data
            ]
            count = 0
            for i in range(20):
                unit = ProgressUnit(
                    index=i, length=20, unit="steps", progress=None, desc=None
                )
                count += unit in all_progress_data
            assert count

    def test_cancel_from_client_queued(self, cancel_from_client_demo):
        with connect(cancel_from_client_demo) as client:
            start = time.time()
            job = client.submit(api_name="/long")
            while not job.done():
                if job.status().code == Status.STARTING:
                    job.cancel()
                    break
            with pytest.raises(CancelledError):
                job.result()
            # The whole prediction takes 10 seconds to run
            # and does not iterate. So this tests that we can cancel
            # halfway through a prediction
            assert time.time() - start < 10
            assert job.status().code == Status.CANCELLED

            job = client.submit(api_name="/iterate")
            iteration_count = 0
            while not job.done():
                if job.status().code == Status.ITERATING:
                    iteration_count += 1
                    if iteration_count == 3:
                        job.cancel()
                        break
                    time.sleep(0.5)
            # Result for iterative jobs will raise there is an exception
            with pytest.raises(CancelledError):
                job.result()
            # The whole prediction takes 10 seconds to run
            # and does not iterate. So this tests that we can cancel
            # halfway through a prediction
            assert time.time() - start < 10

            # Test that we did not iterate all the way to the end
            assert all(o in [0, 1, 2, 3, 4, 5] for o in job.outputs())
            assert job.status().code == Status.CANCELLED

    def test_cancel_subsequent_jobs_state_reset(self, yield_demo):
        with connect(yield_demo) as client:
            job1 = client.submit("abcdefefadsadfs", api_name="/predict")
            time.sleep(3)
            job1.cancel()

            assert len(job1.outputs()) > 0
            assert len(job1.outputs()) < len("abcdefefadsadfs")
            assert job1.status().code == Status.CANCELLED

            job2 = client.submit("abcd", api_name="/predict")
            assert len(job2.outputs()) == 0
            while not job2.done():
                time.sleep(0.1)
            # Ran all iterations from scratch
            assert job2.status().code == Status.FINISHED
            assert len(job2.outputs()) == 4

    @pytest.mark.xfail
    def test_stream_audio(self, stream_audio):
        with connect(stream_audio) as client:
            job1 = client.submit(
                "https://gradio-builds.s3.amazonaws.com/demo-files/bark_demo.mp4",
                api_name="/predict",
            )
            assert Path(job1.result()).exists()

            job2 = client.submit(
                "https://gradio-builds.s3.amazonaws.com/demo-files/audio_sample.wav",
                api_name="/predict",
            )
            assert Path(job2.result()).exists()
            assert all(Path(p).exists() for p in job2.outputs())

    @pytest.mark.xfail
    def test_upload_file_private_space_v4(self):
        client = Client(
            src="gradio-tests/not-actually-private-file-uploadv4-sse",
        )

        with patch.object(
            client.endpoints[0], "_upload", wraps=client.endpoints[0]._upload
        ) as upload:
            with patch.object(
                client.endpoints[0], "serialize", wraps=client.endpoints[0].serialize
            ) as serialize:
                with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
                    f.write("Hello from private space!")

                output = client.submit(
                    1, "foo", f.name, api_name="/file_upload"
                ).result()
            with open(output) as f:
                assert f.read() == "Hello from private space!"
            upload.assert_called_once()
            assert all(f["is_file"] for f in serialize.return_value())

        with patch.object(
            client.endpoints[1], "_upload", wraps=client.endpoints[0]._upload
        ) as upload:
            with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
                f.write("Hello from private space!")

            with open(client.submit(f.name, api_name="/upload_btn").result()) as f:
                assert f.read() == "Hello from private space!"
            upload.assert_called_once()

        with patch.object(
            client.endpoints[2], "_upload", wraps=client.endpoints[0]._upload
        ) as upload:
            # `delete=False` is required for Windows compat
            with tempfile.NamedTemporaryFile(mode="w", delete=False) as f1:
                with tempfile.NamedTemporaryFile(mode="w", delete=False) as f2:
                    f1.write("File1")
                    f2.write("File2")
            r1, r2 = client.submit(
                3,
                [f1.name, f2.name],
                "hello",
                api_name="/upload_multiple",
            ).result()
            with open(r1) as f:
                assert f.read() == "File1"
            with open(r2) as f:
                assert f.read() == "File2"
            upload.assert_called_once()

    @pytest.mark.flaky
    def test_upload_file_private_space(self):
        client = Client(
            src="gradio-tests/not-actually-private-file-upload",
            hf_token=HF_TOKEN,
        )

        with patch.object(
            client.endpoints[0], "serialize", wraps=client.endpoints[0].serialize
        ) as serialize:
            with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
                f.write("Hello from private space!")

            output = client.submit(1, "foo", f.name, api_name="/file_upload").result()
        with open(output) as f:
            assert f.read() == "Hello from private space!"
        assert all(f["is_file"] for f in serialize.return_value())

        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("Hello from private space!")

        with open(client.submit(f.name, api_name="/upload_btn").result()) as f:
            assert f.read() == "Hello from private space!"

        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f1:
            with tempfile.NamedTemporaryFile(mode="w", delete=False) as f2:
                f1.write("File1")
                f2.write("File2")
        r1, r2 = client.submit(
            3,
            [f1.name, f2.name],
            "hello",
            api_name="/upload_multiple",
        ).result()
        with open(r1) as f:
            assert f.read() == "File1"
        with open(r2) as f:
            assert f.read() == "File2"

    @pytest.mark.flaky
    def test_upload_file_upload_route_does_not_exist(self):
        client = Client(
            src="gradio-tests/not-actually-private-file-upload-old-version",
            hf_token=HF_TOKEN,
        )

        with patch.object(
            client.endpoints[0], "serialize", wraps=client.endpoints[0].serialize
        ) as serialize:
            with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
                f.write("Hello from private space!")

                client.submit(1, "foo", f.name, fn_index=0).result()
                serialize.assert_called_once_with(1, "foo", f.name)

    def test_state_without_serialize(self, stateful_chatbot):
        with connect(stateful_chatbot, serialize=False) as client:
            initial_history = [["", None]]
            message = "Hello"
            ret = client.predict(message, initial_history, api_name="/submit")
            assert ret == ("", [["", None], ["Hello", "I love you"]])

    def test_can_call_mounted_app_via_api(self):
        def greet(name):
            return "Hello " + name + "!"

        gradio_app = gr.Interface(
            fn=greet,
            inputs=gr.Textbox(lines=2, placeholder="Name Here..."),
            outputs="text",
        )

        app = FastAPI()
        app = gr.mount_gradio_app(app, gradio_app, path="/test/gradio")
        config = uvicorn.Config(
            app=app,
            port=8000,
            log_level="info",
        )
        server = Server(config=config)
        # Using the gradio Server class to not have
        # to implement code again to run uvicorn in a separate thread
        # However, that means we need to set this flag to prevent
        # run_in_thread_from_blocking
        server.started = True
        try:
            server.run_in_thread()
            time.sleep(1)
            client = Client("http://127.0.0.1:8000/test/gradio/")
            assert client.predict("freddy") == "Hello freddy!"
        finally:
            server.thread.join(timeout=1)

    def test_predict_with_space_with_api_name_false(self):
        client = Client("gradio-tests/client-bool-api-name-error")
        assert client.predict("Hello!", api_name="/run") == "Hello!"
        assert client.predict("Freddy", api_name="/say_hello") == "hello"

    def test_return_layout_component(self, hello_world_with_group):
        with connect(hello_world_with_group) as demo:
            assert demo.predict("Freddy", api_name="/greeting") == "Hello Freddy"
            assert demo.predict(api_name="/show_group") == ()

    def test_return_layout_and_state_components(
        self, hello_world_with_state_and_accordion
    ):
        with connect(hello_world_with_state_and_accordion) as demo:
            assert demo.predict("Freddy", api_name="/greeting") == ("Hello Freddy", 1)
            assert demo.predict("Abubakar", api_name="/greeting") == (
                "Hello Abubakar",
                2,
            )
            assert demo.predict(api_name="/open") == 3
            assert demo.predict(api_name="/close") == 4
            assert demo.predict("Ali", api_name="/greeting") == ("Hello Ali", 5)


class TestStatusUpdates:
    @patch("gradio_client.client.Endpoint.make_end_to_end_fn")
    def test_messages_passed_correctly(self, mock_make_end_to_end_fn, calculator_demo):
        now = datetime.now()

        messages = [
            StatusUpdate(
                code=Status.STARTING,
                eta=None,
                rank=None,
                success=None,
                queue_size=None,
                time=now,
                progress_data=None,
            ),
            StatusUpdate(
                code=Status.SENDING_DATA,
                eta=None,
                rank=None,
                success=None,
                queue_size=None,
                time=now + timedelta(seconds=1),
                progress_data=None,
            ),
            StatusUpdate(
                code=Status.IN_QUEUE,
                eta=3,
                rank=2,
                queue_size=2,
                success=None,
                time=now + timedelta(seconds=2),
                progress_data=None,
            ),
            StatusUpdate(
                code=Status.IN_QUEUE,
                eta=2,
                rank=1,
                queue_size=1,
                success=None,
                time=now + timedelta(seconds=3),
                progress_data=None,
            ),
            StatusUpdate(
                code=Status.ITERATING,
                eta=None,
                rank=None,
                queue_size=None,
                success=None,
                time=now + timedelta(seconds=3),
                progress_data=None,
            ),
            StatusUpdate(
                code=Status.FINISHED,
                eta=None,
                rank=None,
                queue_size=None,
                success=True,
                time=now + timedelta(seconds=4),
                progress_data=None,
            ),
        ]

        class MockEndToEndFunction:
            def __init__(self, communicator: Communicator):
                self.communicator = communicator

            def __call__(self, *args, **kwargs):
                for m in messages:
                    with self.communicator.lock:
                        self.communicator.job.latest_status = m
                    time.sleep(0.1)

        mock_make_end_to_end_fn.side_effect = MockEndToEndFunction

        with connect(calculator_demo) as client:
            job = client.submit(5, "add", 6, api_name="/predict")

            statuses = []
            while not job.done():
                statuses.append(job.status())
                time.sleep(0.09)

            assert all(s in messages for s in statuses)

    @patch("gradio_client.client.Endpoint.make_end_to_end_fn")
    def test_messages_correct_two_concurrent(
        self, mock_make_end_to_end_fn, calculator_demo
    ):
        now = datetime.now()

        messages_1 = [
            StatusUpdate(
                code=Status.STARTING,
                eta=None,
                rank=None,
                success=None,
                queue_size=None,
                time=now,
                progress_data=None,
            ),
            StatusUpdate(
                code=Status.FINISHED,
                eta=None,
                rank=None,
                queue_size=None,
                success=True,
                time=now + timedelta(seconds=4),
                progress_data=None,
            ),
        ]

        messages_2 = [
            StatusUpdate(
                code=Status.IN_QUEUE,
                eta=3,
                rank=2,
                queue_size=2,
                success=None,
                time=now + timedelta(seconds=2),
                progress_data=None,
            ),
            StatusUpdate(
                code=Status.IN_QUEUE,
                eta=2,
                rank=1,
                queue_size=1,
                success=None,
                time=now + timedelta(seconds=3),
                progress_data=None,
            ),
        ]

        class MockEndToEndFunction:
            n_counts = 0

            def __init__(self, communicator: Communicator):
                self.communicator = communicator
                self.messages = (
                    messages_1 if MockEndToEndFunction.n_counts == 0 else messages_2
                )
                MockEndToEndFunction.n_counts += 1

            def __call__(self, *args, **kwargs):
                for m in self.messages:
                    with self.communicator.lock:
                        print(f"here: {m}")
                        self.communicator.job.latest_status = m
                    time.sleep(0.1)

        mock_make_end_to_end_fn.side_effect = MockEndToEndFunction

        with connect(calculator_demo) as client:
            job_1 = client.submit(5, "add", 6, api_name="/predict")
            job_2 = client.submit(11, "subtract", 1, api_name="/predict")

            statuses_1 = []
            statuses_2 = []
            while not (job_1.done() and job_2.done()):
                statuses_1.append(job_1.status())
                statuses_2.append(job_2.status())
                time.sleep(0.05)

            assert all(s in messages_1 for s in statuses_1)


class TestAPIInfo:
    @pytest.mark.parametrize("trailing_char", ["/", ""])
    def test_test_endpoint_src(self, trailing_char):
        src = "https://gradio-calculator.hf.space" + trailing_char
        client = Client(src=src)
        assert client.endpoints[0].root_url == "https://gradio-calculator.hf.space/"

    @pytest.mark.flaky
    def test_numerical_to_label_space(self):
        client = Client("gradio-tests/titanic-survival")
        assert client.view_api(return_format="dict") == {
            "named_endpoints": {
                "/predict": {
                    "parameters": [
                        {
                            "label": "Sex",
                            "type": {"type": "string"},
                            "python_type": {"type": "str", "description": ""},
                            "component": "Radio",
                            "example_input": "Howdy!",
                            "serializer": "StringSerializable",
                        },
                        {
                            "label": "Age",
                            "type": {"type": "number"},
                            "python_type": {"type": "int | float", "description": ""},
                            "component": "Slider",
                            "example_input": 5,
                            "serializer": "NumberSerializable",
                        },
                        {
                            "label": "Fare (british pounds)",
                            "type": {"type": "number"},
                            "python_type": {"type": "int | float", "description": ""},
                            "component": "Slider",
                            "example_input": 5,
                            "serializer": "NumberSerializable",
                        },
                    ],
                    "returns": [
                        {
                            "label": "output",
                            "type": {"type": {}, "description": "any valid json"},
                            "python_type": {
                                "type": "str",
                                "description": "filepath to JSON file",
                            },
                            "component": "Label",
                            "serializer": "JSONSerializable",
                        }
                    ],
                },
                "/predict_1": {
                    "parameters": [
                        {
                            "label": "Sex",
                            "type": {"type": "string"},
                            "python_type": {"type": "str", "description": ""},
                            "component": "Radio",
                            "example_input": "Howdy!",
                            "serializer": "StringSerializable",
                        },
                        {
                            "label": "Age",
                            "type": {"type": "number"},
                            "python_type": {"type": "int | float", "description": ""},
                            "component": "Slider",
                            "example_input": 5,
                            "serializer": "NumberSerializable",
                        },
                        {
                            "label": "Fare (british pounds)",
                            "type": {"type": "number"},
                            "python_type": {"type": "int | float", "description": ""},
                            "component": "Slider",
                            "example_input": 5,
                            "serializer": "NumberSerializable",
                        },
                    ],
                    "returns": [
                        {
                            "label": "output",
                            "type": {"type": {}, "description": "any valid json"},
                            "python_type": {
                                "type": "str",
                                "description": "filepath to JSON file",
                            },
                            "component": "Label",
                            "serializer": "JSONSerializable",
                        }
                    ],
                },
                "/predict_2": {
                    "parameters": [
                        {
                            "label": "Sex",
                            "type": {"type": "string"},
                            "python_type": {"type": "str", "description": ""},
                            "component": "Radio",
                            "example_input": "Howdy!",
                            "serializer": "StringSerializable",
                        },
                        {
                            "label": "Age",
                            "type": {"type": "number"},
                            "python_type": {"type": "int | float", "description": ""},
                            "component": "Slider",
                            "example_input": 5,
                            "serializer": "NumberSerializable",
                        },
                        {
                            "label": "Fare (british pounds)",
                            "type": {"type": "number"},
                            "python_type": {"type": "int | float", "description": ""},
                            "component": "Slider",
                            "example_input": 5,
                            "serializer": "NumberSerializable",
                        },
                    ],
                    "returns": [
                        {
                            "label": "output",
                            "type": {"type": {}, "description": "any valid json"},
                            "python_type": {
                                "type": "str",
                                "description": "filepath to JSON file",
                            },
                            "component": "Label",
                            "serializer": "JSONSerializable",
                        }
                    ],
                },
            },
            "unnamed_endpoints": {},
        }

    def test_state_does_not_appear(self, state_demo):
        with connect(state_demo) as client:
            api_info = client.view_api(return_format="dict")
            assert isinstance(api_info, dict)
            for parameter in api_info["named_endpoints"]["/predict"]["parameters"]:
                assert parameter["component"] != "State"

    @pytest.mark.flaky
    def test_private_space(self):
        client = Client(
            "gradio-tests/not-actually-private-space",
        )
        assert len(client.endpoints) == 3
        assert len([e for e in client.endpoints if e.is_valid]) == 2
        assert len([e for e in client.endpoints if e.is_valid and e.api_name]) == 1
        assert client.view_api(return_format="dict") == {
            "named_endpoints": {
                "/predict": {
                    "parameters": [
                        {
                            "label": "x",
                            "type": {"type": "string"},
                            "python_type": {"type": "str", "description": ""},
                            "component": "Textbox",
                            "example_input": "Howdy!",
                            "serializer": "StringSerializable",
                        }
                    ],
                    "returns": [
                        {
                            "label": "output",
                            "type": {"type": "string"},
                            "python_type": {"type": "str", "description": ""},
                            "component": "Textbox",
                            "serializer": "StringSerializable",
                        }
                    ],
                }
            },
            "unnamed_endpoints": {},
        }

    def test_fetch_fixed_version_space(self, calculator_demo):
        with connect(calculator_demo) as client:
            api_info = client.view_api(return_format="dict")
            assert isinstance(api_info, dict)
            assert api_info["named_endpoints"]["/predict"] == {
                "parameters": [
                    {
                        "label": "num1",
                        "type": {"type": "number"},
                        "python_type": {"type": "float", "description": ""},
                        "component": "Number",
                        "example_input": 3,
                    },
                    {
                        "label": "operation",
                        "type": {
                            "enum": ["add", "subtract", "multiply", "divide"],
                            "title": "Radio",
                            "type": "string",
                        },
                        "python_type": {
                            "type": "Literal[add, subtract, multiply, divide]",
                            "description": "",
                        },
                        "component": "Radio",
                        "example_input": "add",
                    },
                    {
                        "label": "num2",
                        "type": {"type": "number"},
                        "python_type": {"type": "float", "description": ""},
                        "component": "Number",
                        "example_input": 3,
                    },
                ],
                "returns": [
                    {
                        "label": "output",
                        "type": {"type": "number"},
                        "python_type": {"type": "float", "description": ""},
                        "component": "Number",
                    }
                ],
            }
            assert (
                "/load_example" in api_info["named_endpoints"]
            )  # The exact api configuration includes Block IDs and thus is not deterministic
            assert api_info["unnamed_endpoints"] == {}

    def test_unnamed_endpoints_use_fn_index(self, count_generator_demo):
        with connect(count_generator_demo) as client:
            info = client.view_api(return_format="str")
            assert "fn_index" not in info
            assert "api_name" in info

    def test_api_false_endpoints_do_not_appear(self, count_generator_no_api):
        with connect(count_generator_no_api) as client:
            info = client.view_api(return_format="dict")
            assert len(info["named_endpoints"]) == 0

    def test_api_false_endpoints_cannot_be_accessed_with_fn_index(self, increment_demo):
        with connect(increment_demo) as client:
            with pytest.raises(ValueError):
                client.submit(1, fn_index=2)

    def test_file_io(self, file_io_demo):
        with connect(file_io_demo) as client:
            info = client.view_api(return_format="dict")
            inputs = info["named_endpoints"]["/predict"]["parameters"]
            outputs = info["named_endpoints"]["/predict"]["returns"]

            assert inputs[0]["type"]["type"] == "array"
            assert inputs[0]["python_type"] == {
                "type": "List[filepath]",
                "description": "",
            }
            assert isinstance(inputs[0]["example_input"], list)
            assert isinstance(inputs[0]["example_input"][0], str)

            assert inputs[1]["python_type"] == {
                "type": "filepath",
                "description": "",
            }
            assert isinstance(inputs[1]["example_input"], str)

            assert outputs[0]["python_type"] == {
                "type": "List[filepath]",
                "description": "",
            }
            assert outputs[0]["type"]["type"] == "array"

            assert outputs[1]["python_type"] == {
                "type": "filepath",
                "description": "",
            }

    def test_layout_components_in_output(self, hello_world_with_group):
        with connect(hello_world_with_group) as client:
            info = client.view_api(return_format="dict")
            assert info == {
                "named_endpoints": {
                    "/greeting": {
                        "parameters": [
                            {
                                "label": "name",
                                "type": {"type": "string"},
                                "python_type": {"type": "str", "description": ""},
                                "component": "Textbox",
                                "example_input": "Hello!!",
                            }
                        ],
                        "returns": [
                            {
                                "label": "greeting",
                                "type": {"type": "string"},
                                "python_type": {"type": "str", "description": ""},
                                "component": "Textbox",
                            }
                        ],
                    },
                    "/show_group": {"parameters": [], "returns": []},
                },
                "unnamed_endpoints": {},
            }

    def test_layout_and_state_components_in_output(
        self, hello_world_with_state_and_accordion
    ):
        with connect(hello_world_with_state_and_accordion) as client:
            info = client.view_api(return_format="dict")
            assert info == {
                "named_endpoints": {
                    "/greeting": {
                        "parameters": [
                            {
                                "label": "name",
                                "type": {"type": "string"},
                                "python_type": {"type": "str", "description": ""},
                                "component": "Textbox",
                                "example_input": "Hello!!",
                            }
                        ],
                        "returns": [
                            {
                                "label": "greeting",
                                "type": {"type": "string"},
                                "python_type": {"type": "str", "description": ""},
                                "component": "Textbox",
                            },
                            {
                                "label": "count",
                                "type": {"type": "number"},
                                "python_type": {
                                    "type": "float",
                                    "description": "",
                                },
                                "component": "Number",
                            },
                        ],
                    },
                    "/open": {
                        "parameters": [],
                        "returns": [
                            {
                                "label": "count",
                                "type": {"type": "number"},
                                "python_type": {
                                    "type": "float",
                                    "description": "",
                                },
                                "component": "Number",
                            }
                        ],
                    },
                    "/close": {
                        "parameters": [],
                        "returns": [
                            {
                                "label": "count",
                                "type": {"type": "number"},
                                "python_type": {
                                    "type": "float",
                                    "description": "",
                                },
                                "component": "Number",
                            }
                        ],
                    },
                },
                "unnamed_endpoints": {},
            }


class TestEndpoints:
    @pytest.mark.flaky
    def test_upload(self):
        client = Client(
            src="gradio-tests/not-actually-private-file-upload",
        )
        response = MagicMock(status_code=200)
        response.json.return_value = [
            "file1",
            "file2",
            "file3",
            "file4",
            "file5",
            "file6",
            "file7",
        ]
        with patch("requests.post", MagicMock(return_value=response)):
            with patch("builtins.open", MagicMock()):
                with patch.object(pathlib.Path, "name") as mock_name:
                    mock_name.side_effect = lambda x: x
                    results = client.endpoints[0]._upload(
                        ["pre1", ["pre2", "pre3", "pre4"], ["pre5", "pre6"], "pre7"]
                    )

        res = []
        for re in results:
            if isinstance(re, list):
                res.append([r["name"] for r in re])
            else:
                res.append(re["name"])

        assert res == [
            "file1",
            ["file2", "file3", "file4"],
            ["file5", "file6"],
            "file7",
        ]

    @pytest.mark.flaky
    def test_upload_v4(self):
        client = Client(
            src="gradio-tests/not-actually-private-file-uploadv4-sse",
        )
        response = MagicMock(status_code=200)
        response.json.return_value = [
            "file1",
            "file2",
            "file3",
            "file4",
            "file5",
            "file6",
            "file7",
        ]
        with patch("requests.post", MagicMock(return_value=response)):
            with patch("builtins.open", MagicMock()):
                with patch.object(pathlib.Path, "name") as mock_name:
                    mock_name.side_effect = lambda x: x
                    results = client.endpoints[0]._upload(
                        ["pre1", ["pre2", "pre3", "pre4"], ["pre5", "pre6"], "pre7"]
                    )

        res = []
        for re in results:
            if isinstance(re, list):
                res.append([r["path"] for r in re])
            else:
                res.append(re["path"])

        assert res == [
            "file1",
            ["file2", "file3", "file4"],
            ["file5", "file6"],
            "file7",
        ]


cpu = huggingface_hub.SpaceHardware.CPU_BASIC


class TestDuplication:
    @pytest.mark.flaky
    @patch("huggingface_hub.get_space_runtime", return_value=MagicMock(hardware=cpu))
    @patch("gradio_client.client.Client.__init__", return_value=None)
    def test_new_space_id(self, mock_init, mock_runtime):
        Client.duplicate(
            "gradio/calculator",
            "test",
            hf_token=HF_TOKEN,
        )
        mock_runtime.assert_any_call("gradio/calculator", token=HF_TOKEN)
        mock_init.assert_called()
        Client.duplicate(
            "gradio/calculator",
            "gradio-tests/test",
            hf_token=HF_TOKEN,
        )
        mock_runtime.assert_any_call("gradio/calculator", token=HF_TOKEN)
        mock_init.assert_called()

    @pytest.mark.flaky
    @patch("gradio_client.utils.set_space_timeout")
    @patch("huggingface_hub.get_space_runtime", return_value=MagicMock(hardware=cpu))
    @patch("gradio_client.client.Client.__init__", return_value=None)
    def test_dont_set_timeout_if_default_hardware(
        self, mock_init, mock_runtime, mock_set_timeout
    ):
        Client.duplicate(
            "gradio/calculator",
            "test",
        )
        mock_set_timeout.assert_not_called()

    @pytest.mark.flaky
    @patch("huggingface_hub.request_space_hardware")
    @patch("gradio_client.utils.set_space_timeout")
    @patch(
        "huggingface_hub.get_space_runtime",
        return_value=MagicMock(hardware=huggingface_hub.SpaceHardware.CPU_UPGRADE),
    )
    @patch("gradio_client.client.Client.__init__", return_value=None)
    def test_set_timeout_if_not_default_hardware(
        self, mock_init, mock_runtime, mock_set_timeout, mock_request_hardware
    ):
        Client.duplicate(
            "gradio/calculator",
            "test",
            hardware="cpu-upgrade",
            sleep_timeout=15,
            hf_token=HF_TOKEN,
        )
        assert mock_set_timeout.call_count == 1
        _, called_kwargs = mock_set_timeout.call_args
        assert called_kwargs["timeout_in_seconds"] == 15 * 60

    @pytest.mark.flaky
    @patch("huggingface_hub.add_space_secret")
    @patch("huggingface_hub.duplicate_space")
    @patch("gradio_client.client.Client.__init__", return_value=None)
    @patch("gradio_client.utils.set_space_timeout")
    def test_add_secrets(self, mock_time, mock_init, mock_duplicate, mock_add_secret):
        with pytest.raises(RepositoryNotFoundError):
            name = str(uuid.uuid4())
            Client.duplicate(
                "gradio/calculator",
                name,
                hf_token=HF_TOKEN,
                secrets={"test_key": "test_value", "test_key2": "test_value2"},
            )
            mock_add_secret.assert_called_with(
                f"gradio-tests/{name}",
                "test_key",
                "test_value",
                token=HF_TOKEN,
            )
            mock_add_secret.assert_any_call(
                f"gradio-tests/{name}",
                "test_key2",
                "test_value2",
                token=HF_TOKEN,
            )
