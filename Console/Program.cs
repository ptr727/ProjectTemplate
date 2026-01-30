using ptr727.ProjectTemplate.Library;

namespace ptr727.ProjectTemplate.Console;

internal sealed class Program(
    CommandLine.Options commandLineOptions,
    CancellationToken cancellationToken
)
{
    internal CommandLine.Options GetCommandLineOptions() => commandLineOptions;

    internal CancellationToken GetCancellationToken() => cancellationToken;

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

            // Create logger
            _ = LoggerFactory.Create(commandLine.CreateOptions(commandLine.Result).LogOptions);
            Log.Logger.LogOverrideContext().Information("Starting: {Args}", args);

            // Initialize library with static logger
            ILoggerFactory libraryLoggerFactory = LoggerFactory.CreateLoggerFactory();
            LogOptions.SetFactory(libraryLoggerFactory);
            StaticTemplateLibrary.Test();

            // Initialize library with per-instance logger
            TemplateLibrary templateLibrary = new(
                new Options() { LoggerFactory = libraryLoggerFactory }
            );
            templateLibrary.Test();

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
            Log.Information("Executing root command...");
            await Task.Delay(1000, cancellationToken).ConfigureAwait(false);
            return 0;
        }
        catch (Exception ex) when (Log.Logger.LogAndHandle(ex))
        {
            return 1;
        }
    }

    internal async Task<int> ExecuteTestAsync()
    {
        try
        {
            Log.Information("Executing test command...");
            await Task.Delay(1000, cancellationToken).ConfigureAwait(false);
            return 0;
        }
        catch (Exception ex) when (Log.Logger.LogAndHandle(ex))
        {
            return 1;
        }
    }
}
