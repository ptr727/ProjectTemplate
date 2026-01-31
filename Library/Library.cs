namespace ptr727.ProjectTemplate.Library;

/// <summary>
/// Provides the primary library functionality.
/// </summary>
public sealed class TemplateLibrary(Options? options = null)
{
    // Logger is created on first use and then cached
    private readonly Lazy<ILogger> _logger = new(() =>
        LogOptions.CreateLogger<TemplateLibrary>(options)
    );
    internal ILogger Log => _logger.Value;

    /// <summary>
    /// Writes a test log entry to the configured logger.
    /// </summary>
    public void Test() => Log.LogInformation("Test");
}

public static class StaticTemplateLibrary
{
    // Used for naming the logger category
    private sealed class LogCategory;

    // Logger is created on first use and then cached
    private static readonly Lazy<ILogger> s_logger = new(LogOptions.CreateLogger<LogCategory>);
    private static ILogger Log => s_logger.Value;

    /// <summary>
    /// Writes a test log entry to the configured logger.
    /// </summary>
    public static void Test() => Log.LogInformation("Test");
}
