using System.Text;

namespace ptr727.ProjectTemplate.CodeGen;

internal sealed class CodeGenBuilder(string outputPath, CancellationToken cancellationToken)
{
    // Empty runtime -> UtcNow; a fixed value keeps generated output byte-identical to avoid develop->main merge conflicts.
    internal async Task CodeGenAsync(string quote, string runtime)
    {
        string dateTime = string.IsNullOrEmpty(runtime)
            ? DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture)
            : runtime;

        // Codegen example
        string codeGen = $$"""
            namespace ptr727.ProjectTemplate.CodeGen;

            [System.CodeDom.Compiler.GeneratedCode("ptr727.ProjectTemplate.CodeGen", "1.0")]
            internal static class CodeGen
            {
                private const string QuoteOfTheDay = {{ToCSharpStringLiteral(quote)}};

                internal static void Quote()
                {
                    const string dateTime = {{ToCSharpStringLiteral(dateTime)}};
                    Console.WriteLine($"{dateTime} : {QuoteOfTheDay}");
                    Log.Logger.Information("Quote of the Day: {DateTime} : {Quote}", dateTime, QuoteOfTheDay);
                }
            }
            """;

        // Write code to file
        await File.WriteAllTextAsync(outputPath, codeGen, cancellationToken).ConfigureAwait(false);
    }

    private static string ToCSharpStringLiteral(string value)
    {
        StringBuilder sb = new(value.Length + 2);
        _ = sb.Append('"');
        foreach (char c in value)
        {
            _ = sb.Append(
                c switch
                {
                    '\\' => "\\\\",
                    '\"' => "\\\"",
                    '\r' => "\\r",
                    '\n' => "\\n",
                    '\t' => "\\t",
                    '\0' => "\\0",
                    '\b' => "\\b",
                    '\f' => "\\f",
                    '\u2019' => "'",
                    _ when char.IsControl(c) => $"\\u{(int)c:X4}",
                    _ => c.ToString(),
                }
            );
        }
        _ = sb.Append('"');
        return sb.ToString();
    }
}
