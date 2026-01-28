using System.IO;
using Serilog.Sinks.SystemConsole.Themes;

namespace ptr727.ProjectTemplate.CodeGen;

internal sealed class Program(
    CommandLine.Options commandLineOptions,
    CancellationToken cancellationToken
)
{
    internal CommandLine.Options GetCommandLineOptions() => commandLineOptions;

    internal CancellationToken GetCancellationToken() => cancellationToken;

    internal static async Task<int> Main(string[] args)
    {
        // Parse commandline
        CommandLine commandLine = new(args);

        // Bypass startup for errors or help and version commands
        if (CommandLine.BypassStartup(commandLine.Result))
        {
            return await commandLine.Result.InvokeAsync().ConfigureAwait(false);
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
        return await commandLine.Result.InvokeAsync().ConfigureAwait(false);
    }

    internal async Task<int> ExecuteAsync()
    {
        try
        {
            Log.Information("Executing codegen command...");

            string quoteoftheday = "No API key provided.";
            if (!string.IsNullOrEmpty(commandLineOptions.APIKey))
            {
                Log.Information("Retrieving quote from API Ninjas...");
                ApiNinjas apiNinjas = new(commandLineOptions.APIKey, cancellationToken);
                quoteoftheday = await apiNinjas.GetQuoteOfTheDayAsync().ConfigureAwait(false);
            }
            Log.Information("Quote: {Quote}", quoteoftheday);

            string outputPath = Path.Combine(commandLineOptions.CodePath.FullName, "CodeGen.cs");
            Log.Information("Writing quote to {OutputPath}", outputPath);
            CodeGenBuilder codegenBuilder = new(outputPath, cancellationToken);
            await codegenBuilder.CodeGenAsync(quoteoftheday).ConfigureAwait(false);

            return 0;
        }
        catch (Exception ex) when (Log.Logger.LogAndHandle(ex))
        {
            return 1;
        }
    }
}
