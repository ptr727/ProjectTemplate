using Serilog.Sinks.SystemConsole.Themes;

namespace ptr727.ProjectTemplate.CodeGen;

internal sealed class Program(
    CommandLine.Options commandLineOptions,
    CancellationToken cancellationToken
)
{
    internal static async Task<int> Main(string[] args)
    {
        try
        {
            // Parse commandline
            CommandLine commandLine = new(args);
            commandLine.Result.InvocationConfiguration.EnableDefaultExceptionHandler = false;
            commandLine.Result.InvocationConfiguration.ProcessTerminationTimeout = null;

            // Bypass startup for errors or help and version commands
            if (CommandLine.BypassStartup(commandLine.Result))
            {
                return await commandLine.Result.InvokeAsync().ConfigureAwait(false);
            }

            // Log to the console
            LoggerConfiguration loggerConfiguration = new LoggerConfiguration()
                .Enrich.WithThreadId()
                .WriteTo.Console(
                    theme: AnsiConsoleTheme.Code,
                    formatProvider: CultureInfo.InvariantCulture,
                    outputTemplate: "[{Timestamp:HH:mm:ss} {Level:u3}] [t:{ThreadId}{ThreadName}] {Message:lj}{NewLine}{Exception}"
                );
            Log.Logger = loggerConfiguration.CreateLogger();

            // Invoke command
            return await commandLine.Result.InvokeAsync().ConfigureAwait(false);
        }
        catch (Exception ex) when (Log.Logger.LogAndHandle(ex))
        {
            return 1;
        }
        finally
        {
            await Log.CloseAndFlushAsync().ConfigureAwait(false);
        }
    }

    internal async Task<int> ExecuteAsync()
    {
        try
        {
            Log.Information("Executing codegen command...");

            string quoteoftheday = "No API key provided.";
            if (!string.IsNullOrEmpty(commandLineOptions.ApiKey))
            {
                Log.Information("Retrieving quote from API Ninjas...");
                ApiNinjas apiNinjas = new(commandLineOptions.ApiKey, cancellationToken);
                quoteoftheday = await apiNinjas.GetQuoteOfTheDayAsync().ConfigureAwait(false);
            }
            Log.Information("Quote: {Quote}", quoteoftheday);

            string outputPath = Path.Combine(commandLineOptions.CodePath.FullName, "CodeGen.cs");
            Log.Information("Writing quote to {OutputPath}", outputPath);
            CodeGenBuilder codegenBuilder = new(outputPath, cancellationToken);
            await codegenBuilder
                .CodeGenAsync(quoteoftheday, commandLineOptions.Runtime)
                .ConfigureAwait(false);

            return 0;
        }
        catch (Exception ex) when (Log.Logger.LogAndHandle(ex))
        {
            return 1;
        }
    }
}
