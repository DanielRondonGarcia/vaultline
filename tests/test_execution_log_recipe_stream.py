import io
import json
import struct
import tracemalloc
import unittest

from src.worker_agent.infrastructure.security.execution_log_recipe_stream import (
    MAX_RECIPE_BYTES,
    MAX_RECIPE_ITEMS,
    RecipeFrameError,
    encode_recipe_frame,
    read_recipe_frame,
)


class FragmentedStream:
    def __init__(self, data):
        self.stream = io.BytesIO(data)

    def read(self, count):
        return self.stream.read(min(count, 2))


class RecipeFrameTests(unittest.TestCase):
    def test_single_byte_fragments_have_bounded_memory_overhead(self):
        class SingleByteStream:
            def __init__(self, data):
                self.stream = io.BytesIO(data)

            def read(self, count):
                return self.stream.read(min(count, 1))

        value = 'x' * (512 * 1024)
        stream = SingleByteStream(encode_recipe_frame([value]))
        tracemalloc.start()
        try:
            self.assertEqual([value], read_recipe_frame(stream))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 * 1024 * 1024)

    def test_round_trip_reads_partial_chunks_without_consuming_following_output(self):
        source = FragmentedStream(encode_recipe_frame(['secret', 'κλειδί', '']) + b'output')
        self.assertEqual(['secret', 'κλειδί', ''], read_recipe_frame(source))
        self.assertEqual(b'output', source.stream.read())

    def test_empty_list_is_valid(self):
        self.assertEqual([], read_recipe_frame(io.BytesIO(encode_recipe_frame([]))))

    def test_truncated_header_or_body_fails_closed(self):
        frame = encode_recipe_frame(['secret'])
        for length in range(len(frame)):
            with self.subTest(length=length), self.assertRaises(RecipeFrameError):
                read_recipe_frame(io.BytesIO(frame[:length]))

    def test_declared_oversize_rejected_without_reading_body(self):
        stream = io.BytesIO(struct.pack('!I', MAX_RECIPE_BYTES + 1) + b'payload')
        with self.assertRaises(RecipeFrameError):
            read_recipe_frame(stream)
        self.assertEqual(b'payload', stream.read())

    def test_invalid_json_utf8_or_items_fail_closed(self):
        for body in (b'\xff', b'{', b'{}', b'[1]', b'[null]', b'[true]'):
            with self.subTest(body=body), self.assertRaises(RecipeFrameError):
                read_recipe_frame(io.BytesIO(struct.pack('!I', len(body)) + body))

    def test_item_and_byte_limits_are_enforced_in_both_directions(self):
        too_many = ['x'] * (MAX_RECIPE_ITEMS + 1)
        body = json.dumps(too_many).encode()
        with self.assertRaises(RecipeFrameError):
            encode_recipe_frame(too_many)
        with self.assertRaises(RecipeFrameError):
            read_recipe_frame(io.BytesIO(struct.pack('!I', len(body)) + body))
        with self.assertRaises(RecipeFrameError):
            encode_recipe_frame(['x' * (MAX_RECIPE_BYTES + 1)])

    def test_encoder_rejects_invalid_values(self):
        for values in (None, 'secret', [1], [None], ['\ud800']):
            with self.subTest(kind=type(values).__name__), self.assertRaises(RecipeFrameError):
                encode_recipe_frame(values)

    def test_escaped_long_values_cross_encoding_chunks(self):
        value = ('x' * 4095) + '\n"\\' + ('κ' * 4097)
        self.assertEqual([value], read_recipe_frame(io.BytesIO(encode_recipe_frame([value]))))

    def test_escaped_unpaired_surrogate_is_rejected(self):
        body = b'["\\ud800"]'
        with self.assertRaises(RecipeFrameError):
            read_recipe_frame(io.BytesIO(struct.pack('!I', len(body)) + body))

    def test_broken_stream_error_does_not_expose_exception_details(self):
        class Broken:
            def read(self, count):
                raise OSError('secret-should-not-appear')
        with self.assertRaises(RecipeFrameError) as failure:
            read_recipe_frame(Broken())
        self.assertNotIn('secret-should-not-appear', str(failure.exception))
        self.assertTrue(failure.exception.__suppress_context__)

    def test_stream_cannot_return_more_than_requested(self):
        class Incorrect:
            def read(self, count):
                return b'x' * (count + 1)
        with self.assertRaises(RecipeFrameError):
            read_recipe_frame(Incorrect())


if __name__ == '__main__':
    unittest.main()
