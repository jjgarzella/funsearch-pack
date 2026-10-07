import unittest
from engine.funsearch.normalize import extract_idea, normalized_hash


class NormalizeTests(unittest.TestCase):
    def test_comments_whitespace_and_idea(self):
        a = '// IDEA: first\nint f(void) { return 4; }\n'
        b = '// IDEA: second\n int/* a */ f ( void ) { // hi\n return\t4 ; }'
        self.assertEqual(normalized_hash(a), normalized_hash(b))
        self.assertEqual(len(normalized_hash(a)), 64)
        self.assertNotEqual(normalized_hash(a), normalized_hash(a.replace("4", "5")))

    def test_literals_preserved(self):
        source = r'''char *s = "https://x/*not a comment*/ // IDEA: fake"; char c = '/';'''
        self.assertEqual(normalized_hash(source), normalized_hash("/*comment*/ " + source))
        self.assertNotEqual(normalized_hash(source), normalized_hash(source.replace("not a comment", "other")))
        self.assertNotEqual(normalized_hash('char*s="a b";'), normalized_hash('char*s="a  b";'))

    def test_escaped_quotes_and_idea(self):
        source = r'''char *s = "\" // IDEA: fake"; // IDEA: actual
// IDEA: later
'''
        self.assertEqual(extract_idea(source), "actual")
        self.assertEqual(extract_idea('/* // IDEA: hidden */\nint x;'), "")
        self.assertEqual(extract_idea("// IDEA:  trimmed  \n"), "trimmed")

    def test_distinct_tokens_and_directives(self):
        self.assertNotEqual(normalized_hash("x + + y"), normalized_hash("x++ + y"))
        self.assertNotEqual(normalized_hash("int a;"), normalized_hash("inta;"))
        self.assertNotEqual(normalized_hash("#define A 1\nint x;"), normalized_hash("#define A 1 int x;"))
        self.assertEqual(normalized_hash("int f(\nvoid){return 1;}"), normalized_hash("int f(void) { return 1; }"))
        self.assertEqual(normalized_hash("int x = 1 + \\\n2;"), normalized_hash("int x=1+2;"))

    def test_macro_kind_and_preprocessing_numbers(self):
        self.assertNotEqual(normalized_hash("#define F(x) x\n"), normalized_hash("#define F (x) x\n"))
        self.assertEqual(normalized_hash("#define F /**/(x) x\n"), normalized_hash("#define F (x) x\n"))
        self.assertEqual(normalized_hash("#define A /* multiline\ncomment */ 1\n"), normalized_hash("#define A 1\n"))
        self.assertNotEqual(normalized_hash("double x=1e+3;"), normalized_hash("double x=1e + 3;"))

    def test_stringification_and_source_positions_preserve_observable_spacing(self):
        prefix = "#define STR(x) #x\n"
        self.assertNotEqual(normalized_hash(prefix + "sizeof(STR(a+b))"),
                            normalized_hash(prefix + "sizeof(STR(a + b))"))
        for identifier in ("__LINE__", "__builtin_LINE()", "__LI\\\nNE__"):
            with self.subTest(identifier=identifier):
                source = f"int f(void) {{ return {identifier}; }}"
                self.assertNotEqual(normalized_hash(source), normalized_hash("\n" + source))
        # A header or pasted token may introduce an observer absent in this file.
        for prefix in ('#include "candidate.h"\n', "#define JOIN(a,b) a##b\n"):
            with self.subTest(prefix=prefix):
                self.assertNotEqual(normalized_hash(prefix + "F(a+b)"),
                                    normalized_hash(prefix + "F(a + b)"))
        source = 'char *s = "__LINE__"; /* __LINE__ */ int x;'
        self.assertEqual(normalized_hash(source), normalized_hash("\n" + source))
