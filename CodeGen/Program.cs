using System.CommandLine;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Serilog.Sinks.SystemConsole.Themes;

namespace ptr727.ProjectTemplate.CodeGen;

internal sealed class Program(
    CommandLine.Context commandLineContext,
    CancellationToken cancellationToken
)
{
    internal CommandLine.Context GetCommandLineContext() => commandLineContext;

    internal CancellationToken GetCancellationToken() => cancellationToken;

    private readonly HttpClient _httpClient = HttpClientFactory.GetHttpClient();

    internal HttpClient GetHttpClient() => _httpClient;

    internal static async Task<int> Main(string[] args)
    {
        // Parse commandline
        (CommandLine _, RootCommand rootCommand) = await CommandLine
            .CreateRootCommandWithCommandLine()
            .ConfigureAwait(false);
        ParseResult parseResult = rootCommand.Parse(args);

        // Bypass startup for help and version commands
        if (CommandLine.BypassStartup(parseResult))
        {
            return await parseResult.InvokeAsync().ConfigureAwait(false);
        }

        // Configure logging
        LoggerConfiguration loggerConfiguration = new LoggerConfiguration()
            .Enrich.WithThreadId()
            .WriteTo.Console(
                theme: AnsiConsoleTheme.Code,
                formatProvider: CultureInfo.InvariantCulture
            );
        Log.Logger = loggerConfiguration.CreateLogger();

        // Invoke command
        return await parseResult.InvokeAsync().ConfigureAwait(false);
    }

    internal async Task<int> ExecuteAsync()
    {
        try
        {
            string quoteoftheday = "No API key provided.";
            if (!string.IsNullOrEmpty(commandLineContext.APIKey))
            {
                quoteoftheday = await GetQuoteOfTheDayAsync().ConfigureAwait(false);
            }
            await CodeGenAsync(quoteoftheday).ConfigureAwait(false);
            return 0;
        }
        catch (Exception ex) when (Log.Logger.LogAndHandle(ex))
        {
            return 1;
        }
    }

    private async Task<string> GetQuoteOfTheDayAsync()
    {
        // https://api-ninjas.com/api/quotes#v2-quoteoftheday
        using HttpRequestMessage request = new(
            HttpMethod.Get,
            "https://api.api-ninjas.com/v2/quotes?categories=philosophy"
        );
        request.Headers.Add("X-Api-Key", commandLineContext.APIKey);

        using HttpResponseMessage response = await GetHttpClient()
            .SendAsync(request, HttpCompletionOption.ResponseHeadersRead, GetCancellationToken())
            .ConfigureAwait(false);
        _ = response.EnsureSuccessStatusCode();

        using Stream responseStream = await response
            .Content.ReadAsStreamAsync(GetCancellationToken())
            .ConfigureAwait(false);
        QuoteOfTheDayItem[]? items = await JsonSerializer
            .DeserializeAsync(
                responseStream,
                QuoteOfTheDayJsonContext.Default.QuoteOfTheDayItemArray,
                GetCancellationToken()
            )
            .ConfigureAwait(false);

        string? quote = items?.FirstOrDefault()?.Quote;
        return string.IsNullOrWhiteSpace(quote)
            ? throw new InvalidOperationException(
                "Quote of the day response did not include a quote."
            )
            : quote;
    }

    private async Task CodeGenAsync(string quote)
    {
        // Codegen example
        string codeGen = $$"""
            namespace ptr727.ProjectTemplate.CodeGen;

            internal static class CodeGen
            {
                private const string QuoteOfTheDay = {{ToCSharpStringLiteral(quote)}};

                [System.Diagnostics.CodeAnalysis.SuppressMessage(
                    "Globalization",
                    "CA1303:Do not pass literals as localized parameters",
                    Justification = "Demonstration code."
                )]
                internal static void Quote()
                {
                    string dateTime = $"{{DateTime.UtcNow:o}}";
                    Console.WriteLine($"{dateTime} : {QuoteOfTheDay}");
                    Log.Logger.Information("Quote of the Day: {DateTime} : {Quote}", dateTime, QuoteOfTheDay);
                }
            }
            """;

        // Write code to file
        string outputPath = Path.Combine(commandLineContext.CodePath.FullName, "CodeGen.cs");
        await File.WriteAllTextAsync(outputPath, codeGen, GetCancellationToken())
            .ConfigureAwait(false);
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

internal sealed record QuoteOfTheDayItem([property: JsonPropertyName("quote")] string Quote);

[JsonSerializable(typeof(QuoteOfTheDayItem[]))]
internal sealed partial class QuoteOfTheDayJsonContext : JsonSerializerContext;
