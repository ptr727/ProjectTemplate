using System.Text;

namespace ptr727.ProjectTemplate.CodeGen;

internal sealed class CodeGenBuilder(string outputPath, CancellationToken cancellationToken)
{
    // No runtime -> deterministic output from the quote alone (CI). A runtime ("now" -> UtcNow, else the literal value)
    // embeds a timestamp; that per-run state is a local-only demo of why a generator's output then diverges per run.
    internal async Task CodeGenAsync(string quote, string runtime)
    {
        string codeGen = string.IsNullOrEmpty(runtime)
            ? GenerateDeterministic(quote)
            : GenerateWithTimestamp(quote, ResolveRuntime(runtime));

        // Write code to file
        await File.WriteAllTextAsync(outputPath, codeGen, cancellationToken).ConfigureAwait(false);
    }

    private static string ResolveRuntime(string runtime) =>
        runtime.Equals("now", StringComparison.OrdinalIgnoreCase)
            ? DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture)
            : runtime;

    private static string GenerateDeterministic(string quote) =>
        $$"""
            namespace ptr727.ProjectTemplate.CodeGen;

            [System.CodeDom.Compiler.GeneratedCode("ptr727.ProjectTemplate.CodeGen", "1.0")]
            internal static class CodeGen
            {
                private const string QuoteOfTheDay = {{ToCSharpStringLiteral(quote)}};

                internal static void Quote()
                {
                    Console.WriteLine(QuoteOfTheDay);
                    Log.Logger.Information("Quote of the Day: {Quote}", QuoteOfTheDay);
                }
            }
            """;

    private static string GenerateWithTimestamp(string quote, string dateTime) =>
        $$"""
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
