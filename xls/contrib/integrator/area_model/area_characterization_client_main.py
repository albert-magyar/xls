# Lint as: python3
#
# Copyright 2020 The XLS Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Sweeps to characterize datapoints from a synthesis server.

These datapoints can be used in an area model (where they may be interpolated)
-- the results emitted on stdout are in xls.delay_model.DelayModel prototext
format.
"""

import math 

from typing import List, Sequence, Optional, Tuple

from absl import app
from absl import flags
from absl import logging

import grpc

from xls.delay_model import delay_model_pb2
from xls.delay_model import op_module_generator
from xls.ir.python import bits
from xls.synthesis import client_credentials
from xls.synthesis import synthesis_pb2
from xls.synthesis import synthesis_service_pb2_grpc

FLAGS = flags.FLAGS
flags.DEFINE_integer('port', 10000, 'Port to connect to synthesis server on.')

SLE_ALIASES = 'kSLt kSGe kSGt kULe kULt kUGe kNe kEq kNeg kDecode'.split()
FREE_OPS = ('kArray kArrayConcat kArrayIndex kBitSlice kConcat kIdentity'
            'kLiteral kParam kReverse kTuple kTupleIndex kZeroExt').split()

flat_bit_count_sweep_ceiling=65
operand_count_sweep_ceiling=16
base_loop_stride=2

def _synth(stub: synthesis_service_pb2_grpc.SynthesisServiceStub,
           verilog_text: str,
           top_module_name: str) -> synthesis_pb2.CompileResponse:
  request = synthesis_pb2.CompileRequest()
  request.module_text = verilog_text
  request.top_module_name = top_module_name
  logging.vlog(3, '--- Request')
  logging.vlog(3, request)

  return stub.Compile(request)


def _record_area(response: synthesis_pb2.CompileResponse, result: delay_model_pb2.DataPoint):
  cell_histogram = response.instance_count.cell_histogram
  if "SB_LUT4" in cell_histogram:
    result.delay = cell_histogram["SB_LUT4"]
  else:
    result.delay = 0

def _build_data_point(
  op: str, kop: str, num_node_bits: int, num_operand_bits: List[int], 
  stub: synthesis_service_pb2_grpc.SynthesisServiceStub,
  attributes: Sequence[Tuple[str, str]] = (),
  operand_attributes: Sequence[Tuple[str, int]] = (),
  operand_span_attributes: Sequence[Tuple[str, int, int]] = (),
  literal_operand: Optional[int] = None) -> delay_model_pb2.DataPoint:
  """Characterize an operation via synthesis server.

  Args:
    op: Operation name to use for generating an IR package; e.g. 'add'.
    kop: Operation name to emit into datapoints, generally in kConstant form for
      use in the delay model; e.g. 'kAdd'.
    num_node_bits: The number of bits of the operation result.
    num_operand_bits: The number of bits of each operation.
    stub: Handle to the synthesis server.

  Args forwarded to generate_ir_package:
    attributes: Attributes to include in the operation mnemonic. For example,
      "new_bit_count" in extend operations.
    operand_attributes: Attributes consisting of an operand to include in the 
      operation mnemonic. Each element of the sequence specifies the attribute
      name and the index of the first operand (within 'operand_types').
      For example, used for "defualt" in sel operations.
    operand_span_attributes: Attributes consisting of 1 or more operand to include 
      in the operation mnemonic. Each element of the sequence specifies the attribute
      name, the index of the first operand (within 'operand_types'), and 
      the number of operands in the span.
      For example, used for "cases" in sel operations.
    literal_operand: Optionally specifies that the given operand number should
      be substituted with a randomly generated literal instead of a function
      parameter.

  Returns:
    datapoint produced via the synthesis server.
  """
  op_type = f'bits[{num_node_bits}]'
  operand_types = []
  for operand_bit_count in num_operand_bits:
    operand_types.append(f'bits[{operand_bit_count}]')
  ir_text = op_module_generator.generate_ir_package(op, op_type,
                                                    operand_types,
                                                    attributes,
                                                    operand_attributes,
                                                    operand_span_attributes,
                                                    literal_operand) 
  module_name = f'{op}_{num_node_bits}'
  mod_generator_result = op_module_generator.generate_verilog_module(
      module_name, ir_text)
  top_name = module_name + '_wrapper'
  verilog_text = op_module_generator.generate_parallel_module(
      [mod_generator_result], top_name)

  response = _synth(stub, verilog_text, top_name)
  result = delay_model_pb2.DataPoint()
  _record_area(response, result)
  result.operation.op = kop
  result.operation.bit_count = num_node_bits
  for operand_bit_count in num_operand_bits:
    operand = result.operation.operands.add()
    operand.bit_count = operand_bit_count 
  return result

def _run_nary_op(
    op: str, kop: str, stub: synthesis_service_pb2_grpc.SynthesisServiceStub,
    num_inputs: int) -> List[delay_model_pb2.DataPoint]:
  """Characterizes an nary op"""
  results = []
  for bit_count in range(1, flat_bit_count_sweep_ceiling, base_loop_stride):
    print('# nary_op: ' + op + ', ' + str(bit_count) + ' bits, ' + str(num_inputs) + ' inputs')
    results.append(_build_data_point(op, kop,  bit_count, [bit_count]*num_inputs, stub))

  return results

def _run_bin_op_and_add(
    op: str, kop: str, model: delay_model_pb2.DelayModel,
    stub: synthesis_service_pb2_grpc.SynthesisServiceStub) -> None:
  """Runs characterization for the given bin_op and adds it to the model."""
  add_op_model = model.op_models.add(op=kop)
  add_op_model.estimator.lookup.factors.add(
      source=delay_model_pb2.DelayFactor.RESULT_BIT_COUNT)
  model.data_points.extend(_run_nary_op(op, kop, stub, num_inputs=2))

def _run_unary_op_and_add(
    op: str, kop: str, model: delay_model_pb2.DelayModel,
    stub: synthesis_service_pb2_grpc.SynthesisServiceStub) -> None:
  """Runs characterization for the given unary_op and adds it to the model."""
  add_op_model = model.op_models.add(op=kop)
  add_op_model.estimator.lookup.factors.add(
      source=delay_model_pb2.DelayFactor.RESULT_BIT_COUNT)
  model.data_points.extend(_run_nary_op(op, kop, stub, num_inputs=1))

def _run_nary_op_and_add(
    op: str, kop: str, model: delay_model_pb2.DelayModel,
    stub: synthesis_service_pb2_grpc.SynthesisServiceStub) -> None:
  """Runs characterization for the given nary_op and adds it to the model."""
  add_op_model = model.op_models.add(op=kop)
  add_op_model.estimator.lookup.factors.add(
      source=delay_model_pb2.DelayFactor.RESULT_BIT_COUNT)
  add_op_model.estimator.lookup.factors.add(
      source=delay_model_pb2.DelayFactor.OPERAND_COUNT )

  for input_count in range(1,operand_count_sweep_ceiling, base_loop_stride):
    model.data_points.extend(_run_nary_op(op, kop, stub, num_inputs=input_count))

def _run_single_bit_result_op_and_add (
    op: str, kop: str, model: delay_model_pb2.DelayModel,
    stub: synthesis_service_pb2_grpc.SynthesisServiceStub, num_inputs: int) -> None:
  """Runs characterization for the given op that always produce a single-bit output"""
  """and adds it to the model. The op has one or more inputs, all of the same bit type"""
  add_op_model = model.op_models.add(op=kop)
  add_op_model.estimator.lookup.factors.add(
      source = delay_model_pb2.DelayFactor.OPERAND_BIT_COUNT,
      operand_number = 0)

  for input_bits in range(1, flat_bit_count_sweep_ceiling, base_loop_stride):
    print('# reduction_op: ' + op + ', ' + str(input_bits) + ' bits')
    model.data_points.append(_build_data_point(op, kop, 1, [input_bits] * num_inputs, stub))

def _run_reduction_op_and_add(
    op: str, kop: str, model: delay_model_pb2.DelayModel,
    stub: synthesis_service_pb2_grpc.SynthesisServiceStub) -> None:
  return _run_single_bit_result_op_and_add(op, kop, model, stub, num_inputs=1)

def _run_comparison_op_and_add(
    op: str, kop: str, model: delay_model_pb2.DelayModel,
    stub: synthesis_service_pb2_grpc.SynthesisServiceStub) -> None:
  return _run_single_bit_result_op_and_add(op, kop, model, stub, num_inputs=2)

def _run_select_op_and_add (
    op: str, kop: str, model: delay_model_pb2.DelayModel,
    stub: synthesis_service_pb2_grpc.SynthesisServiceStub) -> None:
  """Runs characterization for the select op"""
  add_op_model = model.op_models.add(op=kop)
  add_op_model.estimator.lookup.factors.add(
      source=delay_model_pb2.DelayFactor.RESULT_BIT_COUNT)
  add_op_model.estimator.lookup.factors.add(
      source=delay_model_pb2.DelayFactor.OPERAND_COUNT)

  # Enumerate cases and bitwidth.
  for num_cases in range(2, operand_count_sweep_ceiling, base_loop_stride):
    for bit_count in range(1, flat_bit_count_sweep_ceiling, base_loop_stride):
      print('# select_op: ' + op + ', ' + str(bit_count) + ' bits, ' + str(num_cases) + ' cases')
      cases_span_attribute = ('cases', 1, num_cases)

      # Handle differently if num_cases is a power of 2.
      select_bits = bits.min_bit_count_unsigned(num_cases-1)
      if math.pow(2, select_bits) == num_cases:
        model.data_points.append(_build_data_point(op, kop, bit_count, [select_bits] + ([bit_count] * num_cases), 
          stub, operand_span_attributes=[cases_span_attribute]))
      else:
        default_attribute = ('default', num_cases+1)
        model.data_points.append(_build_data_point(op, kop, bit_count, [select_bits] + ([bit_count] * (num_cases+1)), 
          stub, operand_span_attributes=[cases_span_attribute], operand_attributes=[default_attribute]))

def _run_one_hot_select_op_and_add (
    op: str, kop: str, model: delay_model_pb2.DelayModel,
    stub: synthesis_service_pb2_grpc.SynthesisServiceStub) -> None:
  """Runs characterization for the one hot select op"""
  add_op_model = model.op_models.add(op=kop)
  add_op_model.estimator.lookup.factors.add(
      source=delay_model_pb2.DelayFactor.RESULT_BIT_COUNT)
  add_op_model.estimator.lookup.factors.add(
      source=delay_model_pb2.DelayFactor.OPERAND_COUNT)

  # Enumerate cases and bitwidth.
  for num_cases in range(2, operand_count_sweep_ceiling, base_loop_stride):
    for bit_count in range(1, flat_bit_count_sweep_ceiling, base_loop_stride):
      print('# one_hot_select_op: ' + op + ', ' + str(bit_count) + ' bits, ' + str(num_cases) + ' cases')
      cases_span_attribute = ('cases', 1, num_cases)
      select_bits = num_cases
      model.data_points.append(_build_data_point(op, kop, bit_count, [select_bits] + ([bit_count] * num_cases), 
        stub, operand_span_attributes=[cases_span_attribute]))

def _run_encode_op_and_add (
    op: str, kop: str, model: delay_model_pb2.DelayModel,
    stub: synthesis_service_pb2_grpc.SynthesisServiceStub) -> None:
  """Runs characterization for the encode op"""
  add_op_model = model.op_models.add(op=kop)
  add_op_model.estimator.lookup.factors.add(
      source = delay_model_pb2.DelayFactor.OPERAND_BIT_COUNT,
      operand_number = 0)

  for input_bits in range(2, flat_bit_count_sweep_ceiling, base_loop_stride):
    print('# encode_op: ' + op + ', ' + str(input_bits) + ' input bits')
    node_bits = bits.min_bit_count_unsigned(input_bits-1)
    model.data_points.append(_build_data_point(op, kop, node_bits, [input_bits], stub))

def _run_decode_op_and_add (
    op: str, kop: str, model: delay_model_pb2.DelayModel,
    stub: synthesis_service_pb2_grpc.SynthesisServiceStub) -> None:
  """Runs characterization for the decode op"""
  add_op_model = model.op_models.add(op=kop)
  add_op_model.estimator.lookup.factors.add(
      source = delay_model_pb2.DelayFactor.RESULT_BIT_COUNT)

  for node_bits in range(2, flat_bit_count_sweep_ceiling, base_loop_stride):
    print('# decode_op: ' + op + ', ' + str(node_bits) + ' bits')
    input_bits = bits.min_bit_count_unsigned(node_bits-1)
    model.data_points.append(_build_data_point(op, kop, node_bits, [input_bits], stub, 
      attributes=[('width', str(node_bits))]))

def _run_dynamic_bit_slice_op_and_add(
    op: str, kop: str, model: delay_model_pb2.DelayModel,
    stub: synthesis_service_pb2_grpc.SynthesisServiceStub) -> None:
  """Runs characterization for the dynamic bit slice op"""
  add_op_model = model.op_models.add(op=kop)
  # TODO: Expect area = output_width * 2^start_bits
  add_op_model.estimator.lookup.factors.add(
      source = delay_model_pb2.DelayFactor.RESULT_BIT_COUNT)
  add_op_model.estimator.lookup.factors.add(
      source = delay_model_pb2.DelayFactor.OPERAND_BIT_COUNT,
      operand_number = 0)

  for start_bits in range(2, bits.min_bit_count_unsigned(flat_bit_count_sweep_ceiling - 1), base_loop_stride**2):
    input_bits = 2 ** start_bits
    for node_bits in range(1, input_bits, base_loop_stride**2):
      print('# encode_op: ' + op + ', ' + str(start_bits) + ' start bits, ' + str(input_bits) 
          + ' input_bits, ' + str(node_bits) + ' width')
      model.data_points.append(_build_data_point(op, kop, node_bits, [input_bits, start_bits], stub, 
        attributes=[('width', str(node_bits))]))

def _run_one_hot_op_and_add(
    op: str, kop: str, model: delay_model_pb2.DelayModel,
    stub: synthesis_service_pb2_grpc.SynthesisServiceStub) -> None:
  """Runs characterization for the one hot slice op"""
  add_op_model = model.op_models.add(op=kop)
  add_op_model.estimator.lookup.factors.add(
      source = delay_model_pb2.DelayFactor.OPERAND_BIT_COUNT,
      operand_number = 0)
  for bit_count in range(1, flat_bit_count_sweep_ceiling, base_loop_stride):
    print('# one_hot: ' + op + ', ' + str(bit_count) + ' input bits')
    # lsb / msb priority or the same logic but mirror image.
    model.data_points.append(_build_data_point(op, kop,  bit_count+1, [bit_count], stub,
      attributes=[('lsb_prio', 'true')]))

def _run_sign_ext_op_and_add(
    op: str, kop: str, model: delay_model_pb2.DelayModel,
    stub: synthesis_service_pb2_grpc.SynthesisServiceStub) -> None:
  """Runs characterization for the sign extend op"""
  add_op_model = model.op_models.add(op=kop)
  add_op_model.estimator.lookup.factors.add(
      source = delay_model_pb2.DelayFactor.RESULT_BIT_COUNT)
  add_op_model.estimator.lookup.factors.add(
      source = delay_model_pb2.DelayFactor.OPERAND_BIT_COUNT,
      operand_number = 0)

  for node_count in range(2, flat_bit_count_sweep_ceiling, base_loop_stride * (2**2)):
    for input_count in range(1, node_count - 1, base_loop_stride * (2**2)):
      print('# sign_ext: ' + op + ', ' + str(input_count) + ' input bits, ' + str(node_count) + ' output_bits')
      model.data_points.append(_build_data_point(op, kop, node_count, [input_count], stub,
        attributes=[('new_bit_count', str(node_count))]))


def  _run_mul_op_and_add(
    op: str, kop: str, model: delay_model_pb2.DelayModel,
    stub: synthesis_service_pb2_grpc.SynthesisServiceStub) -> None:
  """Runs characterization for a mul op"""
  add_op_model = model.op_models.add(op=kop)
  add_op_model.estimator.lookup.factors.add(
      source = delay_model_pb2.DelayFactor.RESULT_BIT_COUNT)
  add_op_model.estimator.lookup.factors.add(
      source = delay_model_pb2.DelayFactor.OPERAND_BIT_COUNT,
      operand_number = 0)
  add_op_model.estimator.lookup.factors.add(
      source = delay_model_pb2.DelayFactor.OPERAND_BIT_COUNT,
      operand_number = 1)

  for mplier_count in range(2, flat_bit_count_sweep_ceiling, base_loop_stride * (2**3)):
    for mcand_count in range(2, flat_bit_count_sweep_ceiling, base_loop_stride * (2**3)):
      for node_count in range(2, flat_bit_count_sweep_ceiling, base_loop_stride * (2**3)):
        print('# mul: ' + op + ', ' + str(mplier_count) + ' * ' + str(mcand_count) + 
            ' -> ' + str(node_count))
        model.data_points.append(_build_data_point(op, kop, node_count, [mplier_count, mcand_count], stub))

def run_characterization(
    stub: synthesis_service_pb2_grpc.SynthesisServiceStub) -> None:
  """Runs characterization via 'stub', DelayModel to stdout as prototext."""
  model = delay_model_pb2.DelayModel()

  '''
  # Bin ops
  _run_bin_op_and_add('add', 'kAdd', model, stub)
  _run_bin_op_and_add('sdiv', 'kSDiv', model, stub)
  _run_bin_op_and_add('smod', 'kSMod', model, stub)
  _run_bin_op_and_add('shll', 'kShll', model, stub)
  _run_bin_op_and_add('shrl', 'kShrl', model, stub)
  _run_bin_op_and_add('shra', 'kShra', model, stub)
  _run_bin_op_and_add('sub', 'kSub', model, stub)
  _run_bin_op_and_add('udiv', 'kUDiv', model, stub)
  _run_bin_op_and_add('umod', 'kUMod', model, stub)

  # Unary ops
  _run_unary_op_and_add('neg', 'kNeg', model, stub)
  _run_unary_op_and_add('not', 'kNot', model, stub)

  # Nary ops
  _run_nary_op_and_add('and', 'kAnd', model, stub)
  _run_nary_op_and_add('nand', 'kNAnd', model, stub)
  _run_nary_op_and_add('nor', 'kNor', model, stub)
  _run_nary_op_and_add('or', 'kOr', model, stub)
  _run_nary_op_and_add('xor', 'kXor', model, stub)

  # Reduction ops
  _run_reduction_op_and_add('and_reduce', 'kAndReduce', model, stub)
  _run_reduction_op_and_add('or_reduce', 'kOrReduce', model, stub)
  _run_reduction_op_and_add('xor_reduce', 'kXorReduce', model, stub)

  # Comparison ops
  _run_comparison_op_and_add('eq', 'kEq', model, stub)
  _run_comparison_op_and_add('ne', 'kNe', model, stub)
  _run_comparison_op_and_add('sge', 'kSGe', model, stub)
  _run_comparison_op_and_add('sgt', 'kSGt', model, stub)
  _run_comparison_op_and_add('sle', 'kSLe', model, stub)
  _run_comparison_op_and_add('slt', 'kSLt', model, stub)
  _run_comparison_op_and_add('uge', 'kUGe', model, stub)
  _run_comparison_op_and_add('ugt', 'kUGt', model, stub)
  _run_comparison_op_and_add('ule', 'kULe', model, stub)
  _run_comparison_op_and_add('ult', 'kULt', model, stub)

  # Select ops
  # For functions only called for 1 op, could just encode
  # op and kOp into function.  However, perfer consistency
  # and readability of passing them in as args.
  _run_select_op_and_add('sel', 'kSel', model, stub)
  _run_one_hot_select_op_and_add('one_hot_sel', 'kOneHotSel', model, stub)
  
  # Encode ops
  _run_encode_op_and_add('encode', 'kEncode', model, stub)
  _run_decode_op_and_add('decode', 'kDecode', model, stub)

  # Dynamic ops
  _run_dynamic_bit_slice_op_and_add('dynamic_bit_slice', 'kDynamicBitSlice', model, stub)

  # One hot op
  _run_one_hot_op_and_add('one_hot', 'kOneHot', model, stub)

  # Sign extend op
  _run_sign_ext_op_and_add('sign_ext', 'kSignExt', model, stub)
  '''

  # Mul ops
  _run_mul_op_and_add('smul', 'kSMul', model, stub)
  _run_mul_op_and_add('umul', 'kUMul', model, stub)

  # Add free ops.
  for free_op in FREE_OPS:
    entry = model.op_models.add(op=free_op)
    entry.estimator.fixed = 0

  print('# proto-file: xls/delay_model/delay_model.proto')
  print('# proto-message: xls.delay_model.DelayModel')
  print(model)


def main(argv):
  if len(argv) != 1:
    raise app.UsageError('Unexpected arguments.')

  channel_creds = client_credentials.get_credentials()
  with grpc.secure_channel(f'localhost:{FLAGS.port}', channel_creds) as channel:
    grpc.channel_ready_future(channel).result()
    stub = synthesis_service_pb2_grpc.SynthesisServiceStub(channel)

    run_characterization(stub)


if __name__ == '__main__':
  app.run(main)
