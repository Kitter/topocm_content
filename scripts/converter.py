#!/usr/bin/env python3

import argparse
from itertools import dropwhile
import os
import re
import tarfile
import tempfile
import shutil
import urllib.request
from xml.etree.ElementTree import SubElement
from xml.etree import ElementTree
from pathlib import Path

from ruamel.yaml import YAML
import jinja2
import nbformat
from nbformat import v4 as current
from nbconvert import HTMLExporter

try:
    os.environ['PYTHONPATH'] = os.environ['PYTHONPATH'] + ':./code'
except KeyError:
    os.environ['PYTHONPATH'] = './code'

START_DATE = "2000-01-01T10:00:00Z"  # Any date in the past.

exportHtml = HTMLExporter(config={
    'HTMLExporter': {
        'template_file': 'edx',
        'template_path': ['.', str(Path(__file__).parent)],
        'exclude_input': True,
        'anchor_link_text': ' ',
    },
})

url = (
    "https://cdnjs.cloudflare.com/ajax/libs"
    "/iframe-resizer/3.5.14/iframeResizer.min.js"
)
js = urllib.request.urlopen(url).read().decode('utf-8')

IFRAME_TEMPLATE = r"""
<iframe id="{id}" scrolling="no" width="100%" frameborder=0>
Your browser does not support IFrames.
</iframe>

<script>
var iframe = document.getElementById('{id}');
iframe.src =  "//" +
              (document.domain.endsWith("edge.edx.org") ? "test." : "") +
              "topocondmat.org/edx/{id}.html?date=" + (+ new Date());
</script>

<script>{js}</script>

<script>
if (require === undefined) {{
// Detect IE10 and below
var isOldIE = (navigator.userAgent.indexOf("MSIE") !== -1);
iFrameResize({{
    heightCalculationMethod: isOldIE ? 'max' : 'lowestElement',
    minSize:100,
    log:true,
    checkOrigin:false
    }}, "#{id}");
}} else {{
  require(["{url}"], (iFrameResize) => iFrameResize())
}}
</script>
"""


def split_into_units(nb_name):
    """Split notebook into units where top level headings occur."""
    nb = nbformat.read(str(nb_name), as_version=4)

    # Split markdown cells on titles.
    def split_cells():
        cells = dropwhile(
            (lambda cell: cell.cell_type != 'markdown'),
            nb.cells
        )
        for cell in cells:
            if cell.cell_type != 'markdown':
                yield cell
            else:
                split_sources = re.split(
                    '(^# .*$)', cell.source, flags=re.MULTILINE
                )
                for src in split_sources:
                    yield nbformat.NotebookNode(
                        source=src,
                        cell_type='markdown',
                        metadata={},
                    )

    units = []
    for cell in split_cells():
        if cell.cell_type == 'markdown' and cell.source.startswith('# '):
            nb_name = re.match('^# (.*)$', cell.source).group(1)
            units.append(current.new_notebook(metadata={
                'name': nb_name
            }))
        else:
            if not units:  # We did not encounter a title yet.
                continue
            units[-1].cells.append(cell)

    return units


def convert_normal_cells(normal_cells):
    """ Convert normal_cells into html. """
    tmp = current.new_notebook(cells=normal_cells)
    return exportHtml.from_notebook_node(tmp)[0]


def convert_unit(unit):
    """ Convert unit into html and special xml componenets. """
    cells = unit.cells

    unit_output = []
    normal_cells = []

    for cell in cells:
        # Markdown-like cell
        if cell.cell_type == 'markdown':
            normal_cells.append(cell)
            continue

        # Empty code cell
        if not hasattr(cell, 'outputs'):
            continue

        xml_components = []
        for output in cell.outputs:
            data = output.get('data')
            if data and 'application/vnd.edx.olxml+xml' in data:
                xml_components.append(
                    data['application/vnd.edx.olxml+xml']
                )

        # Regular code cell
        if not xml_components:
            normal_cells.append(cell)
            continue

        if len(xml_components) > 1:
            raise RuntimeError('More than 1 xml component in a cell.')

        # Cells with mooc components, special processing required
        xml = ElementTree.fromstring(xml_components[0])

        if normal_cells:
            html = convert_normal_cells(normal_cells)
            unit_output.append(html)
            normal_cells = []
        unit_output.append(xml)

    if normal_cells:
        html = convert_normal_cells(normal_cells)
        unit_output.append(html)
        normal_cells = []

    return unit_output


def converter(mooc_folder, content_folder=None):
    """ Do converting job. """
    # Mooc content location
    if content_folder is None:
        content_folder = mooc_folder

    # copying figures
    target = mooc_folder / 'generated'
    figures_path = target / 'html/edx/figures'
    figures_path.mkdir(exist_ok=True)
    for figure in content_folder.glob('w*/figures/*'):
        shutil.copy(figure, figures_path)
    html_folder = target / 'html/edx'

    # Temporary locations
    dirpath = Path(tempfile.mkdtemp()) / 'course'

    skeleton = mooc_folder / 'edx_skeleton'
    shutil.copytree(skeleton, dirpath)

    # Loading data from toc
    chapters = YAML().load(Path(mooc_folder / 'toc.yml').read_text())

    # Convert the syllabus and save it in /tabs
    (skeleton / 'tabs' / 'syllabus.html').write_text(
        exportHtml.from_filename(content_folder / 'syllabus.ipynb')[0]
    )

    course_xml_path = dirpath / 'course.xml'
    xml_course = ElementTree.fromstring(course_xml_path.read_text())

    for chapter_number, chapter in enumerate(chapters):
        chapter_xml = SubElement(xml_course, 'chapter', attrib=dict(
            url_name=f"sec_{chapter_number:02}",
            display_name=chapter['title'],
            start=START_DATE,
        ))

        for section_number, section in enumerate(chapter['sections']):
            section_url = f"subsec_{chapter_number:02}_{section_number:02}"
            sequential_xml = SubElement(
                chapter_xml,
                'sequential',
                attrib={
                    'url_name': section_url,
                    'display_name': section['title'],
                    'graded': ('true' if chapter_number else 'false'),
                },
            )

            if section['title'] == 'Assignments':
                sequential_xml.attrib['format'] = "Research"
            elif chapter_number:
                sequential_xml.attrib['format'] = "Self-check"

            units = split_into_units(
                content_folder / (section['location'] + '.ipynb')
            )

            for i, unit in enumerate(units):
                vertical_url = section_url + f'_{i:02}'
                # add vertical info to sequential_xml
                vertical = SubElement(sequential_xml, 'vertical', attrib=dict(
                    url_name=vertical_url,
                    display_name=unit.metadata.name,
                ))

                unit_output = convert_unit(unit)
                for (j, out) in enumerate(unit_output):
                    out_url = vertical_url + f"_out_{j:02}"
                    if isinstance(out, str):
                        # adding html subelement
                        SubElement(vertical, 'html', attrib=dict(
                            url_name=out_url,
                            display_name=unit.metadata.name,
                            filename=out_url
                        ))

                        html_path = html_folder / (out_url + '.html')
                        html_path.write_text(out)

                        html_path = dirpath / 'html' / (out_url + '.html')
                        html_path.write_text(
                            IFRAME_TEMPLATE.format(id=out_url, url=url, js=js)
                        )

                    else:
                        # adding video subelement
                        vertical.append(out)
                        if 'url_name' not in out.attrib:
                            out.attrib['url_name'] = out_url

    course_xml_path.write_text(
        ElementTree.tostring(xml_course, encoding='unicode')
    )

    # Creating tar
    tar_filepath = target / 'import_to_edx.tar.gz'
    tar = tarfile.open(name=tar_filepath, mode='w:gz')
    tar.add(dirpath, arcname='')
    tar.close()

    # Cleaning
    shutil.rmtree(dirpath)


def expand_syllabus(toc, template, out):
    """Plug the TOC data into a syllabus template."""
    Path(out).write_text(
        jinja2.Template(Path(template).read_text())
        .render(chapters=YAML().load(Path(toc)))
    )


def main():
    mooc_folder = Path(__file__).parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument('source', nargs='?', help='folder to convert')
    args = parser.parse_args()

    print('Path to mooc folder:', mooc_folder)
    print('Path to notebooks:', args.source)
    converter(mooc_folder, content_folder=Path(args.source))


if __name__ == "__main__":
    main()
