namespace ptr727.ProjectTemplate.Library;

/// <summary>
/// Provides the primary library functionality.
/// </summary>
public sealed class TemplateLibrary(Options? options = null)
{
    internal ILogger Log { get; } = LogOptions.CreateLogger<TemplateLibrary>(options);

    /// <summary>
    /// Writes a test log entry to the configured logger.
    /// </summary>
    public void Test() => Log.LogInformation("Test");
}

public static class StaticTemplateLibrary
{
    private sealed class LogCategory;

    private static ILogger Log => LogOptions.CreateLogger<LogCategory>();

    /// <summary>
    /// Writes a test log entry to the configured logger.
    /// </summary>
    public static void Test() => Log.LogInformation("Test");
}
