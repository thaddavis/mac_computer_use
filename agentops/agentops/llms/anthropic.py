from functools import cached_property
import pprint
from typing import Optional
import json

from agentops.llms.instrumented_provider import InstrumentedProvider
from agentops.time_travel import fetch_completion_override_from_time_travel_cache

from ..event import ErrorEvent, LLMEvent, ToolEvent
from ..session import Session
from ..log_config import logger
from ..helpers import check_call_stack_for_agent_id, get_ISO_time
from ..singleton import singleton

from anthropic import _legacy_response

from anthropic.resources.beta.messages.messages import MessagesWithRawResponse, AsyncMessagesWithRawResponse

class PatchedMessagesWithRawResponse:
    def __init__(self, messages_instance, patched_create_func):
        self.messages_instance = messages_instance
        self.patched_create_func = patched_create_func

    def create(self, *args, **kwargs):
        # Call the patched function for the raw response
        return self.patched_create_func(*args, **kwargs)

@singleton
class AnthropicProvider(InstrumentedProvider):

    original_create = None
    original_create_async = None

    def __init__(self, client):
        super().__init__(client)
        self._provider_name = "Anthropic"
        self.tool_event = {}
        self.tool_id = ""

    def handle_response(
        self, response, kwargs, init_timestamp, session: Optional[Session] = None
    ):
        print("1")

        """Handle responses for Anthropic"""
        from anthropic import Stream, AsyncStream
        from anthropic.resources import AsyncMessages
        import anthropic.resources.beta.messages.messages as beta_messages
        from anthropic.resources.beta.messages.messages import Messages
        from anthropic.types import Message

        print("2")

        print()
        print("kwargs: ", kwargs)
        print()

        # kwargs = json.dumps(kwargs)
        # kwargs = json.dumps(kwargs, default=str)

        llm_event = LLMEvent(init_timestamp=init_timestamp, params=kwargs)

        print("3")
        if session is not None:
            llm_event.session_id = session.session_id

        def handle_stream_chunk(chunk: Message):
            print("4")

            try:
                # We take the first chunk and accumulate the deltas from all subsequent chunks to build one full chat completion
                if chunk.type == "message_start":
                    llm_event.returns = chunk
                    llm_event.agent_id = check_call_stack_for_agent_id()
                    llm_event.model = kwargs["model"]
                    llm_event.prompt = kwargs["messages"]
                    llm_event.prompt_tokens = chunk.message.usage.input_tokens
                    llm_event.completion = {
                        "role": chunk.message.role,
                        "content": "",  # Always returned as [] in this instance type
                    }

                elif chunk.type == "content_block_start":
                    if chunk.content_block.type == "text":
                        llm_event.completion["content"] += chunk.content_block.text

                    elif chunk.content_block.type == "tool_use":
                        self.tool_id = chunk.content_block.id
                        self.tool_event[self.tool_id] = ToolEvent(
                            name=chunk.content_block.name,
                            logs={"type": chunk.content_block.type, "input": ""},
                        )

                elif chunk.type == "content_block_delta":
                    if chunk.delta.type == "text_delta":
                        llm_event.completion["content"] += chunk.delta.text

                    elif chunk.delta.type == "input_json_delta":
                        self.tool_event[self.tool_id].logs[
                            "input"
                        ] += chunk.delta.partial_json

                elif chunk.type == "content_block_stop":
                    pass

                elif chunk.type == "message_delta":
                    llm_event.completion_tokens = chunk.usage.output_tokens

                elif chunk.type == "message_stop":
                    llm_event.end_timestamp = get_ISO_time()
                    self._safe_record(session, llm_event)

            except Exception as e:
                print("5")

                self._safe_record(
                    session, ErrorEvent(trigger_event=llm_event, exception=e)
                )

                kwargs_str = pprint.pformat(kwargs)
                chunk = pprint.pformat(chunk)
                logger.warning(
                    f"Unable to parse a chunk for LLM call. Skipping upload to AgentOps\n"
                    f"chunk:\n {chunk}\n"
                    f"kwargs:\n {kwargs_str}\n",
                )

        # if the response is a generator, decorate the generator
        if isinstance(response, Stream):
            print("6")

            def generator():
                for chunk in response:
                    handle_stream_chunk(chunk)
                    yield chunk

            return generator()

        # For asynchronous AsyncStream
        if isinstance(response, AsyncStream):
            print("*** For asynchronous AsyncStream ***")

            async def async_generator():
                async for chunk in response:
                    handle_stream_chunk(chunk)
                    yield chunk

            return async_generator()

        # For async AsyncMessages
        if isinstance(response, AsyncMessages):
            print("*** *** ***")
            print("****** For async AsyncMessages ******")  
            print("*** *** ***")

            async def async_generator():
                async for chunk in response:
                    handle_stream_chunk(chunk)
                    yield chunk

            return async_generator()

        # Handle object responses
        try:
            llm_event.returns = response.model_dump()
            llm_event.agent_id = check_call_stack_for_agent_id()
            llm_event.prompt = kwargs["messages"]
            llm_event.prompt_tokens = response.usage.input_tokens
            llm_event.completion = {
                "role": "assistant",
                "content": response.content[0].text,
            }
            llm_event.completion_tokens = response.usage.output_tokens
            llm_event.model = response.model
            llm_event.end_timestamp = get_ISO_time()

            self._safe_record(session, llm_event)
        except Exception as e:
            self._safe_record(session, ErrorEvent(trigger_event=llm_event, exception=e))
            kwargs_str = pprint.pformat(kwargs)
            response = pprint.pformat(response)
            # logger.warning(
            #     f"Unable to parse response for LLM call. Skipping upload to AgentOps\n"
            #     f"response:\n {response}\n"
            #     f"kwargs:\n {kwargs_str}\n"
            # )

        print("888")

        return response

    def override(self):

        self._override_completion()
        self._override_async_completion()

        # Define a new class for the patched `MessagesWithRawResponse`

    def _override_completion(self):
        from anthropic.resources import messages, Beta
        import anthropic.resources.beta.messages.messages as beta_messages
        from anthropic.types import (
            Message,
            RawContentBlockDeltaEvent,
            RawContentBlockStartEvent,
            RawContentBlockStopEvent,
            RawMessageDeltaEvent,
            RawMessageStartEvent,
            RawMessageStopEvent,
        )

        # Store the original method
        self.original_create = messages.Messages.create
        self.original_create_beta = beta_messages.Messages.create
        self.original_with_raw_reponse_create = beta_messages.Messages.with_raw_response
            
        # self.create = _legacy_response.to_raw_response_wrapper(
        #     messages.create,
        # )

        def create_patched_function(is_beta=False, is_raw=False):
            print("10")

            def patched_function(messages_instance, *args, **kwargs):
                init_timestamp = get_ISO_time()
                session = kwargs.get("session", None)
                if "session" in kwargs.keys():
                    del kwargs["session"]

                completion_override = fetch_completion_override_from_time_travel_cache(
                    kwargs
                )
                if completion_override:
                    result_model = None
                    pydantic_models = (
                        Message,
                        RawContentBlockDeltaEvent,
                        RawContentBlockStartEvent,
                        RawContentBlockStopEvent,
                        RawMessageDeltaEvent,
                        RawMessageStartEvent,
                        RawMessageStopEvent,
                    )

                    for pydantic_model in pydantic_models:
                        try:
                            result_model = pydantic_model.model_validate_json(
                                completion_override
                            )
                            break
                        except Exception as e:
                            pass

                    if result_model is None:
                        logger.error(
                            f"Time Travel: Pydantic validation failed for {pydantic_models} \n"
                            f"Time Travel: Completion override was:\n"
                            f"{pprint.pformat(completion_override)}"
                        )
                        return None
                    
                    print("10d")
                    return self.handle_response(
                        result_model, kwargs, init_timestamp, session=session
                    )

                # Call the original function with its original arguments
                original_func = None
                if is_raw and is_beta:
                    original_func = (
                        self.original_with_raw_reponse_create
                    )
                elif is_beta:
                    original_func = self.original_create_beta
                else:
                    original_func = self.original_create

                # original_func = (
                #     self.original_create_beta if is_beta else self.original_create
                # )

                result = original_func(messages_instance, *args, **kwargs)
                return self.handle_response(
                    result, kwargs, init_timestamp, session=session
                )

            return patched_function

        # Override the original methods with the patched ones
        messages.Messages.create = create_patched_function(is_beta=False)
        beta_messages.Messages.create = create_patched_function(is_beta=True)
        # beta_messages.Messages.with_raw_response = create_patched_function(is_beta=True, is_raw=True)
        
        # Patch `with_raw_response` property
        def patched_with_raw_response(self):
            # Automatically pass `self` (the instance of Messages) as `messages_instance`
            return PatchedMessagesWithRawResponse(self, create_patched_function(is_beta=True, is_raw=True).__get__(self))

        # Apply the patched property
        beta_messages.Messages.with_raw_response = property(patched_with_raw_response)

        """
        WTF beta_messages.Messages.with_raw_response.create = create_patched_function(is_beta=True)
        """


    def _override_async_completion(self):
        from anthropic.resources import messages, Beta
        from anthropic.types import (
            Message,
            RawContentBlockDeltaEvent,
            RawContentBlockStartEvent,
            RawContentBlockStopEvent,
            RawMessageDeltaEvent,
            RawMessageStartEvent,
            RawMessageStopEvent,
        )
        import anthropic.resources.beta.messages.messages as beta_messages
        from anthropic.resources.beta.messages.messages import Messages

        # Store the original method
        self.original_create_async = messages.AsyncMessages.create
        self.original_create_async_beta = beta_messages.AsyncMessages.create
        self.original_async_with_raw_reponse = beta_messages.AsyncMessages.with_raw_response

        def create_patched_async_function(is_beta=False, is_raw=False):
            print("11")

            async def patched_function(*args, **kwargs):
                print("11a")
                init_timestamp = get_ISO_time()
                session = kwargs.get("session", None)
                if "session" in kwargs.keys():
                    del kwargs["session"]

                completion_override = fetch_completion_override_from_time_travel_cache(
                    kwargs
                )
                if completion_override:
                    result_model = None
                    pydantic_models = (
                        Message,
                        RawContentBlockDeltaEvent,
                        RawContentBlockStartEvent,
                        RawContentBlockStopEvent,
                        RawMessageDeltaEvent,
                        RawMessageStartEvent,
                        RawMessageStopEvent,
                    )

                    for pydantic_model in pydantic_models:
                        try:
                            result_model = pydantic_model.model_validate_json(
                                completion_override
                            )
                            break
                        except Exception as e:
                            pass

                    if result_model is None:
                        logger.error(
                            f"Time Travel: Pydantic validation failed for {pydantic_models} \n"
                            f"Time Travel: Completion override was:\n"
                            f"{pprint.pformat(completion_override)}"
                        )
                        return None

                    return self.handle_response(
                        result_model, kwargs, init_timestamp, session=session
                    )

                # Call the original function with its original arguments
                original_func = None
                if is_raw and is_beta:
                    # original_func = (
                    #     self.original_async_with_raw_reponse
                    # )
                    original_func = (
                        self.original_async_with_raw_reponse
                    )
                    def patched_with_raw_response(self):
                        return original_func(self)
                    original_func  = cached_property(patched_with_raw_response)
                elif is_beta:
                    original_func = self.original_create_async_beta
                else:
                    original_func = self.original_async_with_raw_reponse

                # Call the original function with its original arguments
                # original_func = (
                #     self.original_create_async_beta
                #     if is_beta
                #     else self.original_create_async
                # )
                
                result = await original_func(*args, **kwargs)
                return self.handle_response(
                    result, kwargs, init_timestamp, session=session
                )

            return patched_function

        # Override the original methods with the patched ones
        messages.AsyncMessages.create = create_patched_async_function(is_beta=False)
        beta_messages.AsyncMessages.create = create_patched_async_function(is_beta=True)
        beta_messages.AsyncMessages.with_raw_response = create_patched_async_function(is_beta=True, is_raw=True)

    def undo_override(self):
        if self.original_create is not None and self.original_create_async is not None:
            from anthropic.resources import messages

            messages.Messages.create = self.original_create
            messages.AsyncMessages.create = self.original_create_async
