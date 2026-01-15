namespace ptr727.ProjectTemplate.Tests;

internal static class Fixture // : IDisposable
{
    // public void Dispose() => GC.SuppressFinalize(this);

    public static string GetSamplesFilePath(string fileName) =>
        Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "../../../../Samples", fileName));
}
