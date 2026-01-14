using System.Collections.Concurrent;

namespace ptr727.ProjectTemplate.Console;

public class ProcessTask
{
    [System.Diagnostics.CodeAnalysis.SuppressMessage("Style", "IDE0290:Use primary constructor")]
    public ProcessTask(Program program) => _program = program;

    private readonly Program _program;

    public Program GetProgram() => _program;

    private static readonly FrozenSet<string> s_allowedExtensions = FrozenSet.Create(
        StringComparer.OrdinalIgnoreCase,
        [".wav", ".mp3", ".m4a"]
    );

    public async Task<int> ExecuteAsync()
    {
        Log.Debug("Starting {Task}", nameof(ProcessTask));

        List<string> fileNameList = await GetFileNameListAsync();
        if (fileNameList.Count == 0)
        {
            return 1;
        }

        List<FileInfo> fileInfoList = await GetFileInfoListAsync(fileNameList);
        return (fileInfoList.Count == fileNameList.Count) ? 0 : 1;
    }

    private Task<List<string>> GetFileNameListAsync()
    {
        Log.Information(
            "Enumerating files in '{DirectoryPath}' ...",
            GetProgram().GetCommandLineContext().Path.FullName
        );
        List<string> fileNameList =
        [
            .. GetProgram()
                .GetCommandLineContext()
                .Path.EnumerateFiles("*", SearchOption.AllDirectories)
                .Where(fileInfo => s_allowedExtensions.Contains(fileInfo.Extension))
                .Select(fileInfo => fileInfo.FullName),
        ];
        Log.Information("Found {FileCount} files to process.", fileNameList.Count);
        return Task.FromResult(fileNameList);
    }

    private async Task<List<FileInfo>> GetFileInfoListAsync(List<string> fileNameList)
    {
        ConcurrentBag<FileInfo> fileInfoList = [];
        await Parallel.ForEachAsync(
            fileNameList,
            GetProgram().GetParallelOptions(),
            async (fileName, cancellationToken) =>
            {
                FileInfo fileInfo = new(fileName);
                if (!fileInfo.Exists)
                {
                    Log.Warning("File does not exist: '{FileName}'", fileName);
                    return;
                }
                fileInfoList.Add(fileInfo);
            }
        );
        Log.Information("Found {FileCount} files to process.", fileInfoList.Count);
        return [.. fileInfoList];
    }
}
