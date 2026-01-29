namespace ptr727.ProjectTemplate.Tests;

[System.Diagnostics.CodeAnalysis.SuppressMessage(
    "Maintainability",
    "CA1515:Consider making public types internal",
    Justification = "https://xunit.net/xunit.analyzers/rules/xUnit1027"
)]
[CollectionDefinition("Sequential Test Collection", DisableParallelization = true)]
public class SequentialCollectionDefinition { }

internal static class Fixture // : IDisposable
{
    // public void Dispose() => GC.SuppressFinalize(this);

    public static string GetSamplesFilePath(string fileName) =>
        Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "../../../../Samples", fileName));
}
