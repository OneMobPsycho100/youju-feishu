import java.nio.file.Path;
import org.ofdrw.converter.export.PDFExporterPDFBox;

/** 将已校验的 OFD 全部页面导出为供合并打印的 PDF。 */
public final class OfdPrint {
    public static void main(String[] args) throws Exception {
        try (PDFExporterPDFBox exporter = new PDFExporterPDFBox(Path.of(args[0]), Path.of(args[1]))) {
            exporter.export();
        }
    }
}
