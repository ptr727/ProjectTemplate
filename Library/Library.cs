namespace ptr727.ProjectTemplate.Library;

/// <summary>
/// Provides the primary library functionality.
/// </summary>
public sealed class TemplateLibrary(Options? options = null)
{
    internal readonly ILogger _log = LogOptions.CreateLogger<TemplateLibrary>(options);

    /// <summary>
    /// Writes a test log entry to the configured logger.
    /// </summary>
    public void Test() => _log.LogInformation("Test");
}

public static class StaticTemplateLibrary
{
    private sealed class LogCategory;

    internal static readonly ILogger s_log = LogOptions.CreateLogger<LogCategory>();

    /// <summary>
    /// Writes a test log entry to the configured logger.
    /// </summary>
    public static void Test() => s_log.LogInformation("Test");
}
