"""
Copyright (c) 2016-present, Facebook, Inc.
All rights reserved.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree. An additional grant
of patent rights can be found in the PATENTS file in the same directory.
"""

import logging
from typing import Any
from spyne.decorator import rpc
from spyne.model.complex import Iterable, ComplexModelBase
from spyne.model.primitive import String
from spyne.service import ServiceBase
from magma.enodebd.devices.device_map import get_device_handler_from_name
from magma.enodebd.devices.device_utils import EnodebDeviceName
from magma.enodebd.exceptions import Tr069Error
from magma.enodebd.state_machines.acs_state_utils import \
    IncorrectDeviceHandlerError
from magma.enodebd.state_machines.enb_acs import EnodebAcsStateMachine
from magma.enodebd.state_machines.enb_acs_pointer import StateMachinePointer
from . import models

# Allow methods without 'self' as first input. Required by spyne
# pylint: disable=no-self-argument

# RPC methods supported by ACS
RPC_METHODS = ['Inform', 'GetRPCMethods', 'TransferComplete']
RPC_RESPONSES = [method + 'Response' for method in RPC_METHODS]
# RPC methods supported by CPE
CPE_RPC_METHODS = ['SetParameterValues',
                   'GetParameterValues',
                   'GetParameterNames',
                   'SetParameterAttributes',
                   'GetParameterAttributes',
                   'AddObject',
                   'DeleteObject',
                   'Download',
                   'Reboot']
CPE_RPC_RESPONSES = [method + 'Response' for method in CPE_RPC_METHODS]
# ACS RPC methods that are not explicitly described by the spec (hence shouldn't
# be advertised by GetRPCMethods). Note: No responses for these
PSEUDO_RPC_METHODS = ['Fault']
# Top-level CWMP header elements. Namespaces should be preserved on these (since
# they are not within other CWMP elements)
TOP_LEVEL_HEADER_ELEMENTS = ['ID', 'HoldRequests']


def fill_response_header(ctx):
    """ Echo message ID from input header to output header, when responding to
        CPE->ACS RPC calls """
    ctx.out_header = models.ID(mustUnderstand='1')
    ctx.out_header.Data = ctx.in_header.Data


class AutoConfigServer(ServiceBase):
    """ TR-069 ACS implementation. The TR-069/CWMP RPC messages are defined, as
        per cwmp-1-0.xsd schema definition, in the RPC decorators below. These
        RPC methods are intended to be called by TR-069-compliant customer
        premesis equipment (CPE), over the SOAP/HTTP interface defined by
        TR-069.

        Per spyne documentation, this class is never instantiated, so all RPC
        functions are implicitly staticmethods. Hence use static class variables
        to hold state.
        This also means that only a single thread can be used (since there are
        no locks).
        Note that staticmethod decorator can't be used in conjunction with rpc
        decorator.
    """
    __out_header__ = models.ID
    __in_header__ = models.ID
    _acs_to_cpe_queue = None
    _cpe_to_acs_queue = None

    """ Set maxEnvelopes to 1, as per TR-069 spec """
    _max_envelopes = 1

    @classmethod
    def set_state_machine_pointer(
        cls,
        state_machine_pointer: StateMachinePointer
    ) -> None:
        cls.state_machine_pointer = state_machine_pointer

    @classmethod
    def get_new_state_machine(
        cls,
        device_name: EnodebDeviceName,
    ) -> EnodebAcsStateMachine:
        """
        Create a new state machine because the previous used data model was
        incorrect
        """
        device_handler_class = get_device_handler_from_name(device_name)
        service = cls.state_machine().service
        stats_mgr = cls.state_machine().stats_manager
        acs_state_machine = device_handler_class(service, stats_mgr)
        return acs_state_machine

    @classmethod
    def _handle_tr069_message(cls, ctx: Any, message: ComplexModelBase) -> Any:
        if not cls.state_machine():
            raise Tr069Error('ACS not given eNB state machine')

        # Log incoming msg
        if hasattr(message, 'as_dict'):
            logging.debug('Handling TR069 message: %s', str(type(message)))
        else:
            logging.debug('Handling TR069 message.')

        try:
            req = cls.state_machine().handle_tr069_message(message)
        except IncorrectDeviceHandlerError as err:
            logging.warning('Incorrect device_handler! Switching to : %s',
                            str(err.device_name))
            cls.set_state_machine(
                cls.get_new_state_machine(err.device_name))
            # Retry with the new state machine
            req = cls.state_machine().handle_tr069_message(message)

        # Log outgoing msg
        if hasattr(req, 'as_dict'):
            logging.debug('Sending TR069 message: %s', str(req.as_dict()))
        else:
            logging.debug('Sending TR069 message.')

        # Set return message name
        ctx.descriptor.out_message.Attributes.sub_name = req.__class__.__name__

        # Set header
        ctx.out_header = models.ID(mustUnderstand='1')
        ctx.out_header.Data = 'null'

        req_out = cls._generate_acs_to_cpe_request_copy(req)
        return req_out

    @staticmethod
    def _generate_empty_acs_to_cpe_request(ctx):
        """ Generate 'empty' request to CPE using dummy message and empty
            message name """
        ctx.descriptor.out_message.Attributes.sub_name = 'EmptyHttp'
        return models.AcsToCpeRequests()

    @staticmethod
    def _generate_acs_to_cpe_request_copy(request):
        """ Create an AcsToCpeRequests instance with all the appropriate
            members set from the input request. AcsToCpeRequests is a union of
            all request messages, so field names match.
        """
        request_out = models.AcsToCpeRequests()
        for parameter in request.get_flat_type_info(request.__class__):
            try:
                setattr(request_out, parameter, getattr(request, parameter))
            except(AttributeError):
                # Allow un-set parameters. If CPE can't handle this, it will
                # respond with an error message
                pass
        return request_out

    @classmethod
    def state_machine(cls) -> EnodebAcsStateMachine:
        return cls.state_machine_pointer.state_machine

    @classmethod
    def set_state_machine(cls, state_machine: EnodebAcsStateMachine) -> None:
        cls.state_machine_pointer.state_machine = state_machine


    # CPE->ACS RPC calls

    @rpc(_returns=Iterable(String),
         _operation_name="GetRPCMethods")
    def get_rpc_methods(ctx):
        """ GetRPCMethods RPC call is terminated here. No need to pass to higher
            layer """
        fill_response_header(ctx)

        for rpc_method in RPC_METHODS:
            yield '%s' % rpc_method

    @rpc(models.Inform,
         _returns=models.InformResponse,
         _body_style="bare",
         _operation_name="Inform",
         _out_message_name="InformResponse")
    def inform(ctx, request):
        """ Inform response generated locally """
        fill_response_header(ctx)
        resp = AutoConfigServer._handle_tr069_message(ctx, request)
        resp.MaxEnvelopes = AutoConfigServer._max_envelopes
        return resp

    @rpc(models.TransferComplete,
         _returns=models.TransferCompleteResponse,
         _body_style="bare",
         _operation_name="TransferComplete",
         _out_message_name="TransferCompleteResponse")
    def transfer_complete(ctx, request):
        fill_response_header(ctx)
        resp = AutoConfigServer._handle_tr069_message(ctx, request)
        resp.MaxEnvelopes = AutoConfigServer._max_envelopes
        return resp

    # Spyne does not handle no input or SimpleModel input for 'bare' function
    # DummyInput is unused
    # pylint: disable=unused-argument
    @rpc(models.DummyInput,
         _returns=models.AcsToCpeRequests,
         _out_message_name="EmptyHttp",
         _body_style='bare',
         _operation_name="EmptyHttp")
    def empty_http(ctx, dummy):
        # Function to handle empty HTTP request
        return AutoConfigServer._handle_tr069_message(ctx, dummy)

    # CPE->ACS responses to ACS->CPE RPC calls

    @rpc(models.SetParameterValuesResponse,
         _returns=models.AcsToCpeRequests,
         _out_message_name="MessageNameToBeReplaced",
         _body_style='bare',
         _operation_name="SetParameterValuesResponse")
    def set_parameter_values_response(ctx, response):
        return AutoConfigServer._handle_tr069_message(ctx, response)

    @rpc(models.GetParameterValuesResponse,
         _returns=models.AcsToCpeRequests,
         _out_message_name="MessageNameToBeReplaced",
         _body_style='bare',
         _operation_name="GetParameterValuesResponse")
    def get_parameter_values_response(ctx, response):
        return AutoConfigServer._handle_tr069_message(ctx, response)

    @rpc(models.GetParameterNamesResponse,
         _returns=models.AcsToCpeRequests,
         _out_message_name="MessageNameToBeReplaced",
         _body_style='bare',
         _operation_name="GetParameterNamesResponse")
    def get_parameter_names_response(ctx, response):
        return AutoConfigServer._handle_tr069_message(ctx, response)

    @rpc(models.SetParameterAttributesResponse,
         _returns=models.AcsToCpeRequests,
         _out_message_name="MessageNameToBeReplaced",
         _body_style='bare',
         _operation_name="SetParameterAttributesResponse")
    def set_parameter_attributes_response(ctx, response):
        return AutoConfigServer._handle_tr069_message(ctx, response)

    @rpc(models.GetParameterAttributesResponse,
         _returns=models.AcsToCpeRequests,
         _out_message_name="MessageNameToBeReplaced",
         _body_style='bare',
         _operation_name="GetParameterAttributesResponse")
    def get_parameter_attributes_response(ctx, response):
        return AutoConfigServer._handle_tr069_message(ctx, response)

    @rpc(models.AddObjectResponse,
         _returns=models.AcsToCpeRequests,
         _out_message_name="MessageNameToBeReplaced",
         _body_style='bare',
         _operation_name="AddObjectResponse")
    def add_object_response(ctx, response):
        return AutoConfigServer._handle_tr069_message(ctx, response)

    @rpc(models.DeleteObjectResponse,
         _returns=models.AcsToCpeRequests,
         _out_message_name="MessageNameToBeReplaced",
         _body_style='bare',
         _operation_name="DeleteObjectResponse")
    def delete_object_response(ctx, response):
        return AutoConfigServer._handle_tr069_message(ctx, response)

    @rpc(models.DownloadResponse,
         _returns=models.AcsToCpeRequests,
         _out_message_name="MessageNameToBeReplaced",
         _body_style='bare',
         _operation_name="DownloadResponse")
    def download_response(ctx, response):
        return AutoConfigServer._handle_tr069_message(ctx, response)

    @rpc(models.RebootResponse,
         _returns=models.AcsToCpeRequests,
         _out_message_name="MessageNameToBeReplaced",
         _body_style='bare',
         _operation_name="RebootResponse")
    def reboot_response(ctx, response):
        return AutoConfigServer._handle_tr069_message(ctx, response)

    @rpc(models.Fault,
         _returns=models.AcsToCpeRequests,
         _out_message_name="MessageNameToBeReplaced",
         _body_style='bare',
         _operation_name="Fault")
    def fault(ctx, response):
        return AutoConfigServer._handle_tr069_message(ctx, response)


def on_method_return_string(ctx):
    """
    By default, spyne adds a namespace to every single XML element.
    There isn't a way to change this behavior, and the spyne-recommended way
    to fix this is by doing string manipulation. The TR-069 spec mandates that
    only the top-level CWMP elements contain namespaces. Hence this
    function is to remove namespaces from all elements except top-level CWMP
    elements (e.g. RPC request/response names, header elements).
    """
    # Format strings for XML tags, corresponding to:
    # 1) Normal start or end tag (without attribute)
    # 2) Open and close tag (when no attributes or sub-structures exist)
    # 3) Tag containing attributes
    # We don't just look for 'cwmp:%s' (with no character after %s) because this
    # would pick up all tags that start with the tag of interest (e.g
    # cwmp:SetParameterAttributes would also match
    # cwmp:SetParameterAttributesStruct)
    XML_FORMAT_STRS = [["cwmp:%s>", "!!!TEMP_MOD!!!:%s>"],
                       ["cwmp:%s/>", "!!!TEMP_MOD!!!:%s/>"],
                       ["cwmp:%s ", "!!!TEMP_MOD!!!:%s "]]
    fields_to_preserve_ns = list(RPC_METHODS) + list(RPC_RESPONSES) + \
        list(CPE_RPC_METHODS) + list(CPE_RPC_RESPONSES) + \
        list(PSEUDO_RPC_METHODS) + list(TOP_LEVEL_HEADER_ELEMENTS)
    for field in fields_to_preserve_ns:
        for formats in XML_FORMAT_STRS:
            orig_str = formats[0] % field
            temp_str = formats[1] % field
            ctx.out_string[0] = ctx.out_string[0].replace(
                orig_str.encode('ascii'), temp_str.encode('ascii'))

    # Also preserve namespace inside strings, e.g. for arrayType="cwmp:..."
    orig_str = "=\"cwmp:"
    temp_str = "=\"!!!TEMP_MOD!!!:"
    ctx.out_string[0] = ctx.out_string[0].replace(
        orig_str.encode('ascii'), temp_str.encode('ascii'))
    orig_str = "=\'cwmp:"
    temp_str = "=\'!!!TEMP_MOD!!!:"
    ctx.out_string[0] = ctx.out_string[0].replace(
        orig_str.encode('ascii'), temp_str.encode('ascii'))


    ctx.out_string[0] = ctx.out_string[0].replace(b'cwmp:', b'')
    ctx.out_string[0] = ctx.out_string[0].replace(b'!!!TEMP_MOD!!!:', b'cwmp:')

    # Special-case handling so that 'EmptyHttp' RPC will be called using
    # completely empty HTTP request (not even containing a SOAP envelope), as
    # per TR-069 spec.
    if(ctx.descriptor.out_message.Attributes.sub_name == 'EmptyHttp'):
        ctx.out_string = [b'']


AutoConfigServer.event_manager.add_listener('method_return_string',
                                            on_method_return_string)
